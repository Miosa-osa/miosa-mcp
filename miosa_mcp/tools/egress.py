"""Egress MCP tools — secrets vault, network policy, audit log, OAuth catalog.

Exposed tool names (all prefixed with ``miosa_``):

Secrets
-------
miosa_secrets_list          — list secret metadata (no plaintext)
miosa_secrets_set           — create secret + optional binding
miosa_secrets_rotate        — rotate secret value
miosa_secrets_delete        — delete secret
miosa_secrets_connect_oauth — start OAuth flow; returns authorize_url + poll URL

Network
-------
miosa_network_allow         — add an allow rule
miosa_network_deny          — add a deny rule
miosa_network_lockdown      — flip policy to enforce mode
miosa_network_observe       — flip policy to audit-only mode
miosa_network_suggestions   — AI-generated allowlist from observed traffic
miosa_network_list_policies — list egress policies
miosa_network_list_rules    — list allowlist rules for a policy

Audit
-----
miosa_audit_query           — query audit events
miosa_audit_get             — get single audit event
miosa_audit_count_by_host   — count events grouped by host

OAuth catalog
-------------
miosa_oauth_providers_list  — list OAuth providers visible to tenant
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

# ---------------------------------------------------------------------------
# API path constants — all rooted at /api/v1 to match how the transport
# appends to https://api.miosa.ai/api/v1 (httpx base_url behaviour: a path
# starting with "/" replaces the entire path component of the base URL, so
# /api/v1/egress/… → correct; /egress/… would drop the /api/v1 prefix).
# ---------------------------------------------------------------------------

_SECRETS_PATH = "/api/v1/egress/secrets"
_BINDINGS_PATH = "/api/v1/egress/bindings"
_OAUTH_PROVIDERS_PATH = "/api/v1/egress/oauth/providers"
_OAUTH_START_PATH = "/api/v1/egress/oauth/start"
_OAUTH_STATUS_PATH = "/api/v1/egress/oauth/status"
_ALLOWLIST_PATH = "/api/v1/egress/allowlist"
_POLICY_PATH = "/api/v1/egress/policies"
_SUGGESTIONS_PATH = "/api/v1/egress/audit/suggestions"
_AUDIT_PATH = "/api/v1/egress/audit"


# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

EGRESS_TOOLS: list[types.Tool] = [
    # ── Secrets ──────────────────────────────────────────────────────────────
    types.Tool(
        name="miosa_secrets_list",
        description=(
            "List secrets visible to the calling tenant. Returns metadata only "
            "(name, type, scope, id) — plaintext values are never returned. "
            "Filter by scope, workspace, or owner identity."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "workspace", "tenant"],
                    "description": "Filter by secret scope (optional)",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Filter to secrets owned by a specific workspace (optional)",
                },
                "owner_user_id": {
                    "type": "string",
                    "description": "Filter to secrets owned by a specific user ID (optional)",
                },
                "external_user_id": {
                    "type": "string",
                    "description": "Filter by your own user identifier (optional)",
                },
                "external_workspace_id": {
                    "type": "string",
                    "description": "Filter by your own workspace identifier (optional)",
                },
            },
        },
    ),
    types.Tool(
        name="miosa_secrets_set",
        description=(
            "Create a secret and optionally bind it to a resource so it is "
            "injected as an environment variable. "
            "Provide expose_as_env + resource_id + resource_type to auto-bind."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable label for the secret (e.g. OPENAI_API_KEY)",
                },
                "value": {
                    "type": "string",
                    "description": "The secret value (stored encrypted)",
                },
                "type": {
                    "type": "string",
                    "enum": ["api_key", "oauth_token", "password", "certificate", "other"],
                    "description": "Secret type (default: api_key)",
                    "default": "api_key",
                },
                "scope": {
                    "type": "string",
                    "enum": ["user", "workspace", "tenant"],
                    "description": "Visibility scope (default: user)",
                    "default": "user",
                },
                "expose_as_env": {
                    "type": "string",
                    "description": "If set, inject the secret as this env-var name into the bound resource",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Resource to bind to (sandbox or computer ID) — required when expose_as_env is set",
                },
                "resource_type": {
                    "type": "string",
                    "enum": ["sandbox", "computer"],
                    "description": "Resource type matching resource_id",
                },
            },
            "required": ["name", "value"],
        },
    ),
    types.Tool(
        name="miosa_secrets_rotate",
        description=(
            "Rotate a secret's value in-place. All existing bindings continue "
            "to reference the same secret_id and pick up the new value automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "secret_id": {
                    "type": "string",
                    "description": "ID of the secret to rotate",
                },
                "new_value": {
                    "type": "string",
                    "description": "Replacement secret value",
                },
                "refresh_token": {
                    "type": "string",
                    "description": "New refresh token (OAuth secrets only, optional)",
                },
                "expires_at": {
                    "type": "string",
                    "description": "ISO 8601 expiry timestamp (optional)",
                },
            },
            "required": ["secret_id", "new_value"],
        },
    ),
    types.Tool(
        name="miosa_secrets_delete",
        description=(
            "Delete a secret and remove all associated bindings. "
            "Resources that were injecting this secret will no longer receive it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "secret_id": {
                    "type": "string",
                    "description": "ID of the secret to delete",
                },
            },
            "required": ["secret_id"],
        },
    ),
    types.Tool(
        name="miosa_secrets_connect_oauth",
        description=(
            "Start an OAuth 2.0 connect flow for the given provider. "
            "Returns authorize_url (which the user must open in a browser) "
            "and a poll_url to check completion status. "
            "Call miosa_audit_query or GET the poll_url until status=completed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "OAuth provider slug (e.g. github, google, slack, linear)",
                },
                "expose_as_env": {
                    "type": "string",
                    "description": "Env-var name to inject the token as once authorized (optional)",
                },
                "scope": {
                    "type": "string",
                    "description": "Space-separated OAuth scopes to request (optional; provider default if omitted)",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Bind the resulting token to this resource (optional)",
                },
                "resource_type": {
                    "type": "string",
                    "enum": ["sandbox", "computer"],
                    "description": "Resource type for the binding (optional)",
                },
            },
            "required": ["provider"],
        },
    ),
    # ── Network ───────────────────────────────────────────────────────────────
    types.Tool(
        name="miosa_network_allow",
        description=(
            "Add an allow rule to the egress allowlist for a specific host. "
            "Optionally restrict by HTTP methods and path glob. "
            "Rules apply to the tenant policy or a specific resource policy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Hostname or domain to allow (e.g. api.openai.com)",
                },
                "methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "HTTP methods to allow (e.g. [GET, POST]). Omit for all methods.",
                },
                "path_glob": {
                    "type": "string",
                    "description": "Path glob pattern to restrict the rule (e.g. /v1/*). Omit for all paths.",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Scope the rule to a specific resource (optional)",
                },
                "policy_id": {
                    "type": "string",
                    "description": "Attach rule to a specific named policy (optional; uses tenant default if omitted)",
                },
            },
            "required": ["host"],
        },
    ),
    types.Tool(
        name="miosa_network_deny",
        description=(
            "Add an explicit deny rule to the egress allowlist for a specific host. "
            "Deny rules take precedence over allow rules. "
            "Use to block known-bad destinations even in observe mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Hostname or domain to deny (e.g. malicious-site.com)",
                },
                "resource_id": {
                    "type": "string",
                    "description": "Scope the rule to a specific resource (optional)",
                },
                "policy_id": {
                    "type": "string",
                    "description": "Attach rule to a specific named policy (optional)",
                },
            },
            "required": ["host"],
        },
    ),
    types.Tool(
        name="miosa_network_lockdown",
        description=(
            "Switch egress policy to enforce mode — all traffic not explicitly "
            "allowed will be blocked. Use miosa_network_allow to whitelist "
            "destinations before calling this."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Scope to a specific resource (optional; applies to tenant policy if omitted)",
                },
                "policy_id": {
                    "type": "string",
                    "description": "Specific policy to lock down (optional)",
                },
            },
        },
    ),
    types.Tool(
        name="miosa_network_observe",
        description=(
            "Switch egress policy to audit-only (observe) mode — traffic is "
            "logged but not blocked. Use this to build an allowlist before "
            "enforcing with miosa_network_lockdown."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Scope to a specific resource (optional; applies to tenant policy if omitted)",
                },
                "policy_id": {
                    "type": "string",
                    "description": "Specific policy to set to observe mode (optional)",
                },
            },
        },
    ),
    types.Tool(
        name="miosa_network_suggestions",
        description=(
            "Return AI-generated allowlist suggestions derived from recently "
            "observed egress traffic. Use these as input to miosa_network_allow "
            "to build a policy before switching to enforce mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Filter suggestions for a specific resource (optional)",
                },
                "since": {
                    "type": "string",
                    "description": "Look-back window, e.g. '7d', '24h', or an ISO 8601 timestamp (default: 7d)",
                    "default": "7d",
                },
            },
        },
    ),
    types.Tool(
        name="miosa_network_list_policies",
        description="List all egress policies in the tenant, with their mode and default effect.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="miosa_network_list_rules",
        description=(
            "List allowlist rules for a specific policy. "
            "Returns host, methods, path_glob, and effect for each rule."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "policy_id": {
                    "type": "string",
                    "description": "Policy ID to list rules for",
                },
            },
            "required": ["policy_id"],
        },
    ),
    # ── Audit ─────────────────────────────────────────────────────────────────
    types.Tool(
        name="miosa_audit_query",
        description=(
            "Query the egress audit log. Returns events with host, action "
            "(allowed/denied), timestamp, and resource attribution. "
            "Supports filtering by resource, host, action, and time range."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Filter to events from a specific resource (optional)",
                },
                "host": {
                    "type": "string",
                    "description": "Filter by destination host (optional)",
                },
                "action": {
                    "type": "string",
                    "enum": ["allowed", "denied"],
                    "description": "Filter by outcome (optional)",
                },
                "since": {
                    "type": "string",
                    "description": "Start of time range — ISO 8601 timestamp or relative (e.g. '1h', '7d') (optional)",
                },
                "until": {
                    "type": "string",
                    "description": "End of time range — ISO 8601 timestamp (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return (optional)",
                },
                "external_user_id": {
                    "type": "string",
                    "description": "Filter by your own user identifier (optional)",
                },
                "external_workspace_id": {
                    "type": "string",
                    "description": "Filter by your own workspace identifier (optional)",
                },
            },
        },
    ),
    types.Tool(
        name="miosa_audit_get",
        description="Fetch a single audit event by its ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "ID of the audit event to retrieve",
                },
            },
            "required": ["event_id"],
        },
    ),
    types.Tool(
        name="miosa_audit_count_by_host",
        description=(
            "Count egress audit events grouped by destination host for a given "
            "resource and time window. Useful for building allowlists or "
            "detecting anomalous traffic patterns."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "resource_id": {
                    "type": "string",
                    "description": "Resource ID (sandbox or computer) to count traffic for",
                },
                "since": {
                    "type": "string",
                    "description": "Look-back window — ISO 8601 timestamp or relative (e.g. '24h', '7d')",
                },
            },
            "required": ["resource_id", "since"],
        },
    ),
    # ── OAuth catalog ─────────────────────────────────────────────────────────
    types.Tool(
        name="miosa_oauth_providers_list",
        description=(
            "List OAuth providers configured and visible to the calling tenant. "
            "Use provider slugs from this list as input to miosa_secrets_connect_oauth."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

ToolContent = types.TextContent | types.ImageContent


def _ok(text: str) -> list[ToolContent]:
    return [types.TextContent(type="text", text=text)]


def _err(msg: str) -> list[ToolContent]:
    return [types.TextContent(type="text", text=f"Error: {msg}")]


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _unwrap(raw: Any) -> Any:
    """Unwrap { data: ... } or single-key envelope if present."""
    if isinstance(raw, dict):
        for key in ("data", "secret", "policy", "rule", "event", "binding"):
            if key in raw and len(raw) <= 2:
                return raw[key]
    return raw


def _unwrap_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw  # type: ignore[return-value]
    if isinstance(raw, dict):
        for key in (
            "data", "secrets", "policies", "rules", "allowlist",
            "events", "audit", "suggestions", "providers", "items",
        ):
            items = raw.get(key)
            if isinstance(items, list):
                return items  # type: ignore[return-value]
    return []


def _fmt_list(items: list[dict[str, Any]], label: str) -> str:
    if not items:
        return f"No {label} found."
    return f"{label.capitalize()}:\n" + "\n".join(
        "  " + json.dumps(item, default=str) for item in items
    )


# ---------------------------------------------------------------------------
# Dispatch handler — called from server._dispatch when name starts miosa_
# ---------------------------------------------------------------------------

async def dispatch_egress(
    transport: Any,
    name: str,
    args: dict[str, Any],
) -> list[types.TextContent | types.ImageContent]:
    """Route egress tool calls to the MIOSA API."""

    # ── Secrets ───────────────────────────────────────────────────────────────

    if name == "miosa_secrets_list":
        params = _strip_none({
            "scope": args.get("scope"),
            "workspace_id": args.get("workspace_id"),
            "owner_user_id": args.get("owner_user_id"),
            "external_user_id": args.get("external_user_id"),
            "external_workspace_id": args.get("external_workspace_id"),
        })
        raw = await transport.request(
            "GET", _SECRETS_PATH, params=params or None
        )
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "secrets"))

    if name == "miosa_secrets_set":
        if not args.get("name") or not args.get("value"):
            return _err("name and value are required")
        body: dict[str, Any] = {
            "name": args["name"],
            "value": args["value"],
            "type": args.get("type", "api_key"),
            "scope": args.get("scope", "user"),
        }
        body.update(_strip_none({
            "expose_as_env": args.get("expose_as_env"),
            "resource_id": args.get("resource_id"),
            "resource_type": args.get("resource_type"),
        }))
        raw = await transport.request("POST", _SECRETS_PATH, json_body=body)
        secret = _unwrap(raw)
        sid = secret.get("id") if isinstance(secret, dict) else "?"
        return _ok(
            f"Secret created: id={sid}  name={args['name']!r}  type={args.get('type', 'api_key')}"
        )

    if name == "miosa_secrets_rotate":
        sid = args.get("secret_id")
        if not sid:
            return _err("secret_id is required")
        new_val = args.get("new_value")
        if not new_val:
            return _err("new_value is required")
        body = {"value": new_val}
        body.update(_strip_none({
            "refresh_token": args.get("refresh_token"),
            "expires_at": args.get("expires_at"),
        }))
        raw = await transport.request(
            "PATCH", f"{_SECRETS_PATH}/{sid}", json_body=body
        )
        return _ok(f"Secret {sid} rotated successfully.")

    if name == "miosa_secrets_delete":
        sid = args.get("secret_id")
        if not sid:
            return _err("secret_id is required")
        await transport.request("DELETE", f"{_SECRETS_PATH}/{sid}")
        return _ok(f"Secret {sid} deleted.")

    if name == "miosa_secrets_connect_oauth":
        provider = args.get("provider")
        if not provider:
            return _err("provider is required")
        body = {"provider": provider}
        body.update(_strip_none({
            "expose_as_env": args.get("expose_as_env"),
            "scope": args.get("scope"),
            "resource_id": args.get("resource_id"),
            "resource_type": args.get("resource_type"),
        }))
        raw = await transport.request(
            "POST", _OAUTH_START_PATH, json_body=body
        )
        payload = _unwrap(raw) if isinstance(raw, dict) else {}
        authorize_url = str(
            payload.get("authorize_url") or payload.get("authorizeUrl") or ""
        )
        state = str(payload.get("state") or "")
        # Build a poll URL the agent can inspect — it's the status endpoint
        # with the state token as query param (informational, not callable via MCP)
        poll_url = f"{_OAUTH_STATUS_PATH}?state={state}" if state else ""
        result = {
            "authorize_url": authorize_url,
            "state": state,
            "poll_url": poll_url,
            "provider": provider,
            "instructions": (
                "1. Open authorize_url in the user's browser to grant consent. "
                "2. Poll GET /api/v1/egress/oauth/status?state=<state> until "
                "status=completed. The response will include secret_id."
            ),
        }
        return _ok(json.dumps(result, indent=2))

    # ── Network ───────────────────────────────────────────────────────────────

    if name == "miosa_network_allow":
        host = args.get("host")
        if not host:
            return _err("host is required")
        body = {"host": host, "effect": "allow"}
        body.update(_strip_none({
            "methods": args.get("methods"),
            "path_glob": args.get("path_glob"),
            "resource_id": args.get("resource_id"),
            "policy_id": args.get("policy_id"),
        }))
        raw = await transport.request(
            "POST", _ALLOWLIST_PATH, json_body=body
        )
        rule = _unwrap(raw)
        rid = rule.get("id") if isinstance(rule, dict) else "?"
        return _ok(f"Allow rule created: id={rid}  host={host!r}")

    if name == "miosa_network_deny":
        host = args.get("host")
        if not host:
            return _err("host is required")
        body = {"host": host, "effect": "deny"}
        body.update(_strip_none({
            "resource_id": args.get("resource_id"),
            "policy_id": args.get("policy_id"),
        }))
        raw = await transport.request(
            "POST", _ALLOWLIST_PATH, json_body=body
        )
        rule = _unwrap(raw)
        rid = rule.get("id") if isinstance(rule, dict) else "?"
        return _ok(f"Deny rule created: id={rid}  host={host!r}")

    if name == "miosa_network_lockdown":
        policy_id = args.get("policy_id") or None
        resource_id = args.get("resource_id") or None
        if policy_id:
            raw = await transport.request(
                "PATCH", f"{_POLICY_PATH}/{policy_id}", json_body={"mode": "enforce"}
            )
        else:
            patch_body: dict[str, Any] = {"mode": "enforce"}
            if resource_id:
                patch_body["resource_id"] = resource_id
            raw = await transport.request(
                "PATCH", _POLICY_PATH, json_body=patch_body
            )
        policy = _unwrap(raw)
        pid = policy.get("id") if isinstance(policy, dict) else "?"
        return _ok(f"Policy {pid} set to enforce (lockdown) mode.")

    if name == "miosa_network_observe":
        policy_id = args.get("policy_id") or None
        resource_id = args.get("resource_id") or None
        if policy_id:
            raw = await transport.request(
                "PATCH", f"{_POLICY_PATH}/{policy_id}", json_body={"mode": "audit_only"}
            )
        else:
            patch_body = {"mode": "audit_only"}
            if resource_id:
                patch_body["resource_id"] = resource_id
            raw = await transport.request(
                "PATCH", _POLICY_PATH, json_body=patch_body
            )
        policy = _unwrap(raw)
        pid = policy.get("id") if isinstance(policy, dict) else "?"
        return _ok(f"Policy {pid} set to audit_only (observe) mode.")

    if name == "miosa_network_suggestions":
        params = _strip_none({
            "resource_id": args.get("resource_id"),
            "since": args.get("since", "7d"),
        })
        raw = await transport.request(
            "GET", _SUGGESTIONS_PATH, params=params or None
        )
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "suggestions"))

    if name == "miosa_network_list_policies":
        raw = await transport.request("GET", _POLICY_PATH)
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "policies"))

    if name == "miosa_network_list_rules":
        policy_id = args.get("policy_id")
        if not policy_id:
            return _err("policy_id is required")
        params = {"policy_id": policy_id}
        raw = await transport.request(
            "GET", _ALLOWLIST_PATH, params=params
        )
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "rules"))

    # ── Audit ─────────────────────────────────────────────────────────────────

    if name == "miosa_audit_query":
        params = _strip_none({
            "resource_id": args.get("resource_id"),
            "host": args.get("host"),
            "action": args.get("action"),
            "since": args.get("since"),
            "until": args.get("until"),
            "limit": args.get("limit"),
            "external_user_id": args.get("external_user_id"),
            "external_workspace_id": args.get("external_workspace_id"),
        })
        raw = await transport.request(
            "GET", _AUDIT_PATH, params=params or None
        )
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "audit events"))

    if name == "miosa_audit_get":
        event_id = args.get("event_id")
        if not event_id:
            return _err("event_id is required")
        raw = await transport.request("GET", f"{_AUDIT_PATH}/{event_id}")
        event = _unwrap(raw)
        return _ok(json.dumps(event, indent=2, default=str))

    if name == "miosa_audit_count_by_host":
        resource_id = args.get("resource_id")
        since = args.get("since")
        if not resource_id:
            return _err("resource_id is required")
        if not since:
            return _err("since is required")
        params = _strip_none({
            "resource_id": resource_id,
            "since": since,
            "group_by": "host",
        })
        raw = await transport.request(
            "GET", _AUDIT_PATH, params=params
        )
        items = _unwrap_list(raw)
        # Build a summary count map; items may already be pre-grouped by the API
        # or may be raw events we aggregate client-side.
        host_counts: dict[str, int] = {}
        for item in items:
            h = item.get("host") or item.get("destination") or "unknown"
            host_counts[h] = host_counts.get(h, 0) + item.get("count", 1)
        if not host_counts:
            return _ok("No audit events found for the given filters.")
        lines = ["Egress count by host:"]
        for host_val, count in sorted(
            host_counts.items(), key=lambda x: x[1], reverse=True
        ):
            lines.append(f"  {count:6d}  {host_val}")
        return _ok("\n".join(lines))

    # ── OAuth catalog ─────────────────────────────────────────────────────────

    if name == "miosa_oauth_providers_list":
        raw = await transport.request("GET", _OAUTH_PROVIDERS_PATH)
        items = _unwrap_list(raw)
        return _ok(_fmt_list(items, "OAuth providers"))

    # Should never reach here — server._dispatch routes misses to _err
    raise ValueError(f"Unknown egress tool: {name}")
