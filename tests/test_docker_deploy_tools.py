"""Unit tests for Docker Deploy MCP tools."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from miosa_mcp.server import _dispatch, _looks_like_miosa_gateway_json


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.request = AsyncMock(side_effect=self._request)

    async def _request(self, method: str, path: str, **_kwargs: Any) -> Any:
        return self.responses[(method, path)]


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._transport = FakeTransport(responses)


def _text(result: list[Any]) -> str:
    assert result
    return result[0].text


@pytest.mark.asyncio
async def test_sandbox_deploy_docker_posts_template_id_to_native_endpoint() -> None:
    client = FakeClient(
        {
            ("POST", "/api/v1/sandboxes/sbx_123/deploy"): {
                "data": {
                    "deployment_id": "dep_123",
                    "url": "https://clinic.osa.miosa.app",
                    "deployment_product": "docker_deploy",
                    "data": {"deployment": {"docker_deploy_host_id": "ddh_123"}},
                }
            }
        }
    )

    result = await _dispatch(
        client,  # type: ignore[arg-type]
        "sandbox_deploy_docker",
        {
            "sandbox_id": "sbx_123",
            "name": "clinic",
            "port": 3000,
            "docker_deploy_template_id": "nextjs-refero-design-pack",
        },
    )

    assert "dep_123" in _text(result)
    client._transport.request.assert_awaited_once()
    method, path = client._transport.request.call_args.args
    kwargs = client._transport.request.call_args.kwargs
    assert method == "POST"
    assert path == "/api/v1/sandboxes/sbx_123/deploy"
    assert kwargs["json_body"]["deployment_type"] == "docker_deploy"
    assert kwargs["json_body"]["docker_deploy_template_id"] == "nextjs-refero-design-pack"


@pytest.mark.asyncio
async def test_docker_deploy_doctor_returns_structured_control_plane_proof() -> None:
    client = FakeClient(
        {
            ("GET", "/api/v1/deployments/dep_123"): {
                "data": {
                    "id": "dep_123",
                    "name": "Clinic",
                    "deployment_product": "docker_deploy",
                    "docker_deploy_host_id": "ddh_123",
                    "public_url": "https://clinic.osa.miosa.app",
                    "metadata": {"runtime": {"ip": "172.16.74.246", "port": 23906}},
                }
            },
            ("GET", "/api/v1/docker-deploy/hosts/ddh_123"): {
                "data": {"id": "ddh_123", "status": "active", "appliance_status": "healthy"}
            },
        }
    )

    result = await _dispatch(
        client,  # type: ignore[arg-type]
        "docker_deploy_doctor",
        {"deployment_id": "dep_123", "probe": False},
    )

    payload = json.loads(_text(result))
    assert payload["ok"] is True
    assert payload["host"]["id"] == "ddh_123"
    assert {check["name"] for check in payload["checks"]} >= {
        "deployment_product",
        "docker_deploy_host_id",
        "docker_deploy_host_health",
        "route_runtime",
        "public_url",
    }


def test_gateway_json_detection_matches_platform_health_response() -> None:
    assert _looks_like_miosa_gateway_json('{"ok":true,"run_id":"ciq-123"}')
    assert not _looks_like_miosa_gateway_json('{"ok":true,"product":"clinic"}')
