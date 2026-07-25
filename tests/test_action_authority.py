"""Behavioral tests for the MCP action-authority execution boundary."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import mcp.types as types
import pytest

from miosa_mcp.authority import (
    MCP_TOOL_CAPABILITIES,
    ActionAuthorityError,
    authorize_mcp_tool,
    fingerprint,
)
from miosa_mcp.server import build_server


class FakeTransport:
    def __init__(self, decision: dict[str, Any]) -> None:
        self.decision = decision
        self.request = AsyncMock(side_effect=self._request)

    async def _request(self, method: str, path: str, **_kwargs: Any) -> Any:
        if (method, path) == ("GET", "/api/v1/actions/catalog"):
            return {
                "data": [
                    {
                        "name": "sandbox.create",
                        "version": "1.0.0",
                        "fingerprint": _capability_fingerprint("sandbox.create"),
                    }
                ]
            }
        if (method, path) == ("POST", "/api/v1/actions/authorize"):
            return self.decision
        raise AssertionError(f"unexpected request: {method} {path}")


def _capability_fingerprint(name: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(f"miosa-capability/{name}@1.0.0".encode()).hexdigest()


def test_fingerprint_is_canonical_across_key_order() -> None:
    assert fingerprint({"b": [{"z": True, "a": None}], "a": 1}) == fingerprint(
        {"a": 1, "b": [{"a": None, "z": True}]}
    )


@pytest.mark.asyncio
async def test_allowed_mcp_action_uses_server_catalog_and_surface_bound_fingerprint() -> None:
    transport = FakeTransport({"decision": "allow", "receipt_id": "receipt-1"})

    result = await authorize_mcp_tool(
        transport,
        "sandbox_create",
        {"workspace_id": "workspace-1", "template": "base"},
    )

    assert result == {"decision": "allow", "receipt_id": "receipt-1"}
    authorize_call = transport.request.await_args_list[1]
    body = authorize_call.kwargs["json_body"]
    assert body["capability"]["name"] == "sandbox.create"
    assert body["surface"] == "mcp"
    assert body["workspace_id"] == "workspace-1"
    assert body["params_fingerprint"] == fingerprint(
        {"workspace_id": "workspace-1", "template": "base"}
    )


@pytest.mark.asyncio
async def test_pending_approval_fails_closed_before_mcp_dispatch() -> None:
    transport = FakeTransport(
        {
            "decision": "pending_approval",
            "approval_request_id": "approval-42",
            "receipt_id": "receipt-42",
        }
    )

    with pytest.raises(ActionAuthorityError, match="approval-42"):
        await authorize_mcp_tool(transport, "sandbox_create", {"template": "base"})


@pytest.mark.asyncio
async def test_public_mcp_call_returns_an_error_without_dispatch_while_pending() -> None:
    transport = FakeTransport(
        {
            "decision": "pending_approval",
            "approval_request_id": "approval-public",
            "receipt_id": "receipt-public",
        }
    )
    client = type("FakeClient", (), {"_transport": transport})()
    app = build_server(client)
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="sandbox_create",
            arguments={"template": "base"},
        )
    )

    result = await app.request_handlers[types.CallToolRequest](request)

    assert result.root.isError is True
    assert "approval-public" in result.root.content[0].text
    assert len(transport.request.await_args_list) == 2


@pytest.mark.asyncio
async def test_unknown_tool_fails_closed_without_contacting_authority() -> None:
    transport = FakeTransport({"decision": "allow"})

    with pytest.raises(ActionAuthorityError, match="no canonical capability"):
        await authorize_mcp_tool(transport, "not_a_registered_tool", {})

    transport.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_registered_mcp_tool_has_exactly_one_canonical_capability() -> None:
    app = build_server(type("FakeClient", (), {})())
    request = types.ListToolsRequest()
    result = await app.request_handlers[types.ListToolsRequest](request)
    registered = {tool.name for tool in result.root.tools}

    assert set(MCP_TOOL_CAPABILITIES) == registered
    assert MCP_TOOL_CAPABILITIES["sandbox_python"] == "sandbox.python"
