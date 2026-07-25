"""Contract tests for organization context, runs, attribution, and errors."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import mcp.types as types
import pytest
from miosa import AsyncMiosa, MiosaError

from miosa_mcp.server import _dispatch, _error_result, _set_tenant_context, build_server


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], Any] | None = None) -> None:
        self.responses = responses or {}
        self._client = httpx.AsyncClient(headers={"Authorization": "Bearer test"})
        self.request = AsyncMock(side_effect=self._request)

    async def _request(self, method: str, path: str, **_kwargs: Any) -> Any:
        return self.responses[(method, path)]


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any] | None = None) -> None:
        self._transport = FakeTransport(responses)


class FakeSandbox:
    id = "sbx_123"
    state = "running"
    data = {
        "id": id,
        "state": state,
        "external_workspace_id": "workspace_customer_1",
        "external_user_id": "user_customer_1",
        "external_project_id": "project_customer_1",
    }


class FakeSandboxes:
    def __init__(self) -> None:
        self.create = AsyncMock(return_value=FakeSandbox())


class FakeSandboxClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.sandboxes = FakeSandboxes()


def _sdk(client: FakeClient) -> AsyncMiosa:
    return cast(AsyncMiosa, client)


def _payload(result: list[Any]) -> Any:
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_public_contract_tools_are_registered() -> None:
    app = build_server(MagicMock())
    result = await app.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    assert isinstance(result.root, types.ListToolsResult)
    tools = {tool.name: tool for tool in result.root.tools}

    assert tools.keys() >= {
        "organization_list",
        "organization_current",
        "organization_switch",
        "run_create",
        "run_list",
        "run_get",
        "run_cancel",
        "run_outputs",
        "run_messages",
        "run_files",
    }
    assert tools["run_create"].inputSchema["properties"].keys() >= {
        "external_workspace_id",
        "external_user_id",
        "external_project_id",
    }
    assert tools["sandbox_create"].inputSchema["properties"].keys() >= {
        "size",
        "workspace_id",
        "external_workspace_id",
        "external_user_id",
        "external_project_id",
    }
    size_schema = tools["sandbox_create"].inputSchema["properties"]["size"]
    assert size_schema["enum"] == ["xs", "small", "medium", "large", "xl"]
    assert size_schema["default"] == "small"
    assert tools["sandbox_create"].inputSchema["dependentRequired"] == {
        "cpu_count": ["memory_mb", "disk_size_mb"],
        "memory_mb": ["cpu_count", "disk_size_mb"],
        "disk_size_mb": ["cpu_count", "memory_mb"],
    }


@pytest.mark.asyncio
async def test_organization_switch_validates_then_propagates_tenant_header() -> None:
    client = FakeClient(
        {
            ("GET", "/api/v1/platform/tenants/current"): {
                "id": "tenant_123",
                "slug": "panther-defense",
                "name": "Panther Defense",
            }
        }
    )

    try:
        result = await _dispatch(
            _sdk(client),
            "organization_switch",
            {"organization": "panther-defense"},
        )

        assert _payload(result)["organization"]["slug"] == "panther-defense"
        assert client._transport._client.headers["X-MIOSA-Tenant"] == "panther-defense"
        request = client._transport.request.await_args
        assert request is not None
        assert request.kwargs["headers"] == {"X-MIOSA-Tenant": "panther-defense"}
    finally:
        _set_tenant_context(_sdk(client), None)
        await client._transport._client.aclose()


@pytest.mark.asyncio
async def test_run_create_uses_canonical_runs_contract_with_attribution() -> None:
    client = FakeClient(
        {
            ("POST", "/api/v1/runs"): {
                "data": {"id": "run_123", "status": "queued", "target_kind": "sandbox"}
            }
        }
    )

    result = await _dispatch(
        _sdk(client),
        "run_create",
        {
            "instruction": "Build and test the app",
            "target_kind": "sandbox",
            "target_id": "sbx_123",
            "runner": "codex",
            "external_workspace_id": "workspace_customer_1",
            "external_user_id": "user_customer_1",
            "external_project_id": "project_customer_1",
        },
    )

    assert _payload(result)["id"] == "run_123"
    request = client._transport.request.await_args
    assert request is not None
    body = request.kwargs["json_body"]
    assert body["sandbox_id"] == "sbx_123"
    assert body["external_workspace_id"] == "workspace_customer_1"
    assert body["external_user_id"] == "user_customer_1"
    assert body["external_project_id"] == "project_customer_1"
    await client._transport._client.aclose()


@pytest.mark.asyncio
async def test_sandbox_create_propagates_all_attribution_fields() -> None:
    client = FakeSandboxClient()

    result = await _dispatch(
        _sdk(client),
        "sandbox_create",
        {
            "name": "review",
            "workspace_id": "ws_123",
            "external_workspace_id": "workspace_customer_1",
            "external_user_id": "user_customer_1",
            "external_project_id": "project_customer_1",
        },
    )

    client.sandboxes.create.assert_awaited_once_with(
        name="review",
        workspace_id="ws_123",
        external_workspace_id="workspace_customer_1",
        external_user_id="user_customer_1",
        external_project_id="project_customer_1",
    )
    assert _payload(result)["sandbox"]["external_user_id"] == "user_customer_1"
    await client._transport._client.aclose()


@pytest.mark.asyncio
async def test_sandbox_create_propagates_lifecycle_policy_and_canonical_ownership() -> None:
    client = FakeSandboxClient()

    await _dispatch(
        _sdk(client),
        "sandbox_create",
        {
            "size": "small",
            "timeout_sec": 3600,
            "idle_timeout_sec": 900,
            "persistent": True,
            "always_on": False,
            "allow_provision": True,
            "env": {"NODE_ENV": "test"},
            "region": "us-east-1",
            "workspace_slug": "clinic-iq",
            "workspace_name": "Clinic IQ",
            "project_slug": "agent-runtime",
            "project_name": "Agent Runtime",
        },
    )

    client.sandboxes.create.assert_awaited_once_with(
        size="small",
        timeout_sec=3600,
        idle_timeout_sec=900,
        persistent=True,
        always_on=False,
        allow_provision=True,
        env={"NODE_ENV": "test"},
        region="us-east-1",
        workspace_slug="clinic-iq",
        workspace_name="Clinic IQ",
        project_slug="agent-runtime",
        project_name="Agent Runtime",
    )
    await client._transport._client.aclose()


@pytest.mark.asyncio
async def test_sandbox_create_omits_size_to_use_server_default_small() -> None:
    client = FakeSandboxClient()

    await _dispatch(_sdk(client), "sandbox_create", {})

    client.sandboxes.create.assert_awaited_once_with()
    await client._transport._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["xs", "small", "medium", "large", "xl"])
async def test_sandbox_create_serializes_canonical_named_size(size: str) -> None:
    client = FakeSandboxClient()

    await _dispatch(_sdk(client), "sandbox_create", {"size": size})

    client.sandboxes.create.assert_awaited_once_with(size=size)
    await client._transport._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size,cpu_count,memory_mb,disk_size_mb",
    [
        ("xs", 1, 2_048, 10_240),
        ("small", 2, 4_096, 10_240),
        ("medium", 4, 8_192, 20_480),
        ("large", 8, 16_384, 40_960),
        ("xl", 16, 32_768, 81_920),
    ],
)
async def test_sandbox_create_normalizes_exact_legacy_dimensions_to_size(
    size: str, cpu_count: int, memory_mb: int, disk_size_mb: int
) -> None:
    client = FakeSandboxClient()

    await _dispatch(
        _sdk(client),
        "sandbox_create",
        {
            "cpu_count": cpu_count,
            "memory_mb": memory_mb,
            "disk_size_mb": disk_size_mb,
        },
    )

    client.sandboxes.create.assert_awaited_once_with(size=size)
    await client._transport._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"cpu_count": 2, "memory_mb": 4_096}, "require cpu_count, memory_mb"),
        (
            {"cpu_count": 2, "memory_mb": 4_096, "disk_size_mb": 20_480},
            "exactly match a named size contract",
        ),
        (
            {
                "size": "small",
                "cpu_count": 1,
                "memory_mb": 2_048,
                "disk_size_mb": 10_240,
            },
            "match xs, not requested size small",
        ),
        ({"size": "xlarge"}, "Unsupported sandbox size"),
    ],
)
async def test_sandbox_create_rejects_noncanonical_resource_requests(
    arguments: dict[str, Any], message: str
) -> None:
    client = FakeSandboxClient()

    with pytest.raises(ValueError, match=message):
        await _dispatch(_sdk(client), "sandbox_create", arguments)

    client.sandboxes.create.assert_not_awaited()
    await client._transport._client.aclose()


@pytest.mark.asyncio
async def test_deployment_create_returns_url_and_attribution_without_flattening() -> None:
    client = FakeClient(
        {
            ("POST", "/api/v1/deployments"): {
                "data": {
                    "id": "dep_123",
                    "public_url": "https://review.panther-defense.com",
                    "external_user_id": "user_customer_1",
                }
            }
        }
    )

    result = await _dispatch(
        _sdk(client),
        "deployment_create",
        {
            "name": "review",
            "external_workspace_id": "workspace_customer_1",
            "external_user_id": "user_customer_1",
            "external_project_id": "project_customer_1",
        },
    )

    payload = _payload(result)["deployment"]
    assert payload["public_url"] == "https://review.panther-defense.com"
    assert payload["external_user_id"] == "user_customer_1"
    request = client._transport.request.await_args
    assert request is not None
    body = request.kwargs["json_body"]
    assert body["external_user_id"] == "user_customer_1"
    await client._transport._client.aclose()


def test_miosa_errors_are_returned_as_mcp_structured_errors() -> None:
    result = _error_result(
        "organization_current",
        MiosaError(
            "credential cannot use that organization",
            status_code=403,
            code="INVALID_TENANT_CONTEXT",
            request_id="req_123",
            body={"error": {"reason": "not_tenant_member"}},
        ),
    )

    assert result.isError is True
    assert result.structuredContent == {
        "ok": False,
        "error": {
            "type": "MiosaError",
            "code": "INVALID_TENANT_CONTEXT",
            "message": "credential cannot use that organization",
            "status": 403,
            "request_id": "req_123",
            "details": {"error": {"reason": "not_tenant_member"}},
            "tool": "organization_current",
        },
    }
