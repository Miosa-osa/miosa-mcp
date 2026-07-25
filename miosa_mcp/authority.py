"""Fail-closed MIOSA action-authority adapter for MCP tool calls."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any, Protocol


class Transport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        files: Any | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        raw_response: bool = False,
    ) -> Any: ...


class ActionAuthorityError(ValueError):
    """A governed MCP tool was not authorized to execute."""


def _load_tool_capabilities() -> dict[str, str]:
    """Load generated surface aliases without importing authorization policy."""
    path = files("miosa_mcp").joinpath("data/action-capabilities.json")
    contract = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for capability in contract["capabilities"]:
        for tool_name in capability["surfaces"].get("mcp", []):
            if tool_name in mapping:
                raise RuntimeError(f"duplicate MCP action capability alias: {tool_name}")
            mapping[tool_name] = capability["name"]
    if not mapping:
        raise RuntimeError("MCP action capability contract contains no tools")
    return mapping


# This generated adapter map contains identity aliases only.
# Risk, scope, approval posture, versions, and limits remain server-owned.
MCP_TOOL_CAPABILITIES = _load_tool_capabilities()


def canonical_json(value: Any) -> str:
    """Return the cross-language canonical JSON used for authority fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return f"sha256:{digest}"


async def authorize_mcp_tool(
    transport: Transport,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """Authorize one governed MCP call immediately before dispatch.

    Every registered tool must have a canonical capability.
    Tools fail closed on missing aliases, missing catalog entries, stale capability
    versions, pending approval, denial, or control-plane errors.
    """

    capability_name = MCP_TOOL_CAPABILITIES.get(tool_name)
    if capability_name is None:
        raise ActionAuthorityError(
            f"MCP tool {tool_name!r} has no canonical capability and cannot execute"
        )

    catalog_response = await transport.request("GET", "/api/v1/actions/catalog")
    catalog = _unwrap_data(catalog_response)
    if not isinstance(catalog, list):
        raise ActionAuthorityError("action catalog returned an invalid response")

    capability = next(
        (
            entry
            for entry in catalog
            if isinstance(entry, dict) and entry.get("name") == capability_name
        ),
        None,
    )
    if not capability or not isinstance(capability.get("fingerprint"), str):
        raise ActionAuthorityError(
            f"action catalog has no registered capability named {capability_name}"
        )

    public_arguments = {
        key: value for key, value in arguments.items() if not str(key).startswith("__")
    }
    params_fingerprint = fingerprint(public_arguments)
    workspace_id = arguments.get("workspace_id")
    surface = "mcp"
    invocation = {
        "capability": capability["fingerprint"],
        "params": params_fingerprint,
        "surface": surface,
        "tool": tool_name,
        "workspace_id": workspace_id,
    }
    body: dict[str, Any] = {
        "capability": {
            "name": capability_name,
            "fingerprint": capability["fingerprint"],
        },
        "request_fingerprint": fingerprint(invocation),
        "params_fingerprint": params_fingerprint,
        "surface": surface,
    }
    if workspace_id:
        body["workspace_id"] = workspace_id

    response = await transport.request("POST", "/api/v1/actions/authorize", json_body=body)
    decision = _unwrap_data(response)
    if not isinstance(decision, dict):
        raise ActionAuthorityError("action authority returned an invalid response")

    if decision.get("decision") == "allow":
        return decision
    if decision.get("decision") == "pending_approval":
        approval_id = decision.get("approval_request_id", "unknown")
        raise ActionAuthorityError(
            f"central approval required before this action can run (approval {approval_id})"
        )
    if decision.get("decision") == "deny":
        reason = decision.get("reason")
        suffix = f": {reason}" if reason else ""
        raise ActionAuthorityError(f"central action authority denied this action{suffix}")

    raise ActionAuthorityError("action authority returned an unknown decision")


def _unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value
