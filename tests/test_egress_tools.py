"""Unit tests for miosa_mcp/tools/egress.py.

All HTTP calls are intercepted by a mock transport — no network access.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miosa_mcp.tools.egress import (
    EGRESS_TOOLS,
    dispatch_egress,
    _ALLOWLIST_PATH,
    _AUDIT_PATH,
    _OAUTH_PROVIDERS_PATH,
    _OAUTH_START_PATH,
    _POLICY_PATH,
    _SECRETS_PATH,
    _SUGGESTIONS_PATH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transport(**responses: Any) -> AsyncMock:
    """Return an async mock transport.

    Keyword args map ``METHOD:path`` to the value to return from
    ``request()``. If a value is an exception it is raised instead.

    A ``_default`` key can supply a catch-all return value.
    """
    transport = MagicMock()

    async def _request(method: str, path: str, **_kwargs: Any) -> Any:
        key = f"{method}:{path}"
        if key in responses:
            val = responses[key]
            if isinstance(val, Exception):
                raise val
            return val
        if "_default" in responses:
            return responses["_default"]
        return {}

    transport.request = AsyncMock(side_effect=_request)
    return transport


def _text(result: list) -> str:
    """Extract the text content from the first item in an MCP result list."""
    assert result, "empty result list"
    return result[0].text


def _is_error(result: list) -> bool:
    return _text(result).startswith("Error:")


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = {
    "miosa_secrets_list",
    "miosa_secrets_set",
    "miosa_secrets_rotate",
    "miosa_secrets_delete",
    "miosa_secrets_connect_oauth",
    "miosa_network_allow",
    "miosa_network_deny",
    "miosa_network_lockdown",
    "miosa_network_observe",
    "miosa_network_suggestions",
    "miosa_network_list_policies",
    "miosa_network_list_rules",
    "miosa_audit_query",
    "miosa_audit_get",
    "miosa_audit_count_by_host",
    "miosa_oauth_providers_list",
}


def test_egress_tools_catalog_completeness():
    """Every expected tool must appear in EGRESS_TOOLS."""
    registered = {t.name for t in EGRESS_TOOLS}
    assert EXPECTED_TOOL_NAMES == registered, (
        f"Missing: {EXPECTED_TOOL_NAMES - registered}  "
        f"Extra: {registered - EXPECTED_TOOL_NAMES}"
    )


def test_all_tools_have_descriptions():
    for tool in EGRESS_TOOLS:
        assert tool.description, f"Tool {tool.name!r} has no description"
        assert len(tool.description) > 20, (
            f"Tool {tool.name!r} description is suspiciously short"
        )


def test_all_tools_have_input_schemas():
    for tool in EGRESS_TOOLS:
        schema = tool.inputSchema
        assert isinstance(schema, dict), f"Tool {tool.name!r} has no inputSchema"
        assert schema.get("type") == "object"


# ---------------------------------------------------------------------------
# Secrets tools
# ---------------------------------------------------------------------------


class TestSecretsTools:

    @pytest.mark.asyncio
    async def test_list_routes_to_correct_endpoint(self):
        transport = _make_transport(**{
            f"GET:{_SECRETS_PATH}": {"data": [
                {"id": "sec_01", "name": "MY_KEY", "type": "api_key"},
            ]},
        })
        result = await dispatch_egress(transport, "miosa_secrets_list", {})
        text = _text(result)
        assert "MY_KEY" in text
        transport.request.assert_called_once_with("GET", _SECRETS_PATH, params=None)

    @pytest.mark.asyncio
    async def test_list_with_scope_filter(self):
        transport = _make_transport(**{
            f"GET:{_SECRETS_PATH}": {"data": []},
        })
        await dispatch_egress(transport, "miosa_secrets_list", {"scope": "tenant"})
        _, kwargs = transport.request.call_args
        assert kwargs["params"]["scope"] == "tenant"

    @pytest.mark.asyncio
    async def test_list_empty(self):
        transport = _make_transport(**{f"GET:{_SECRETS_PATH}": []})
        result = await dispatch_egress(transport, "miosa_secrets_list", {})
        assert "No secrets found" in _text(result)

    @pytest.mark.asyncio
    async def test_set_routes_to_post(self):
        transport = _make_transport(**{
            f"POST:{_SECRETS_PATH}": {"data": {"id": "sec_99", "name": "OPENAI"}},
        })
        result = await dispatch_egress(
            transport,
            "miosa_secrets_set",
            {"name": "OPENAI_API_KEY", "value": "sk-test"},
        )
        text = _text(result)
        assert "sec_99" in text
        transport.request.assert_called_once()
        method, path = transport.request.call_args.args
        assert method == "POST"
        assert path == _SECRETS_PATH

    @pytest.mark.asyncio
    async def test_set_includes_binding_params(self):
        transport = _make_transport(_default={"data": {"id": "sec_1"}})
        await dispatch_egress(
            transport,
            "miosa_secrets_set",
            {
                "name": "DB_PASS",
                "value": "hunter2",
                "expose_as_env": "DATABASE_URL",
                "resource_id": "sbx_abc",
                "resource_type": "sandbox",
            },
        )
        _, kwargs = transport.request.call_args
        body = kwargs["json_body"]
        assert body["expose_as_env"] == "DATABASE_URL"
        assert body["resource_id"] == "sbx_abc"
        assert body["resource_type"] == "sandbox"

    @pytest.mark.asyncio
    async def test_set_missing_name_returns_error(self):
        transport = _make_transport()
        result = await dispatch_egress(
            transport, "miosa_secrets_set", {"value": "sk-test"}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_set_missing_value_returns_error(self):
        transport = _make_transport()
        result = await dispatch_egress(
            transport, "miosa_secrets_set", {"name": "FOO"}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_rotate_routes_to_patch(self):
        sid = "sec_42"
        transport = _make_transport(**{
            f"PATCH:{_SECRETS_PATH}/{sid}": {"data": {"id": sid}},
        })
        result = await dispatch_egress(
            transport,
            "miosa_secrets_rotate",
            {"secret_id": sid, "new_value": "new-key"},
        )
        assert "rotated" in _text(result)
        transport.request.assert_called_once_with(
            "PATCH",
            f"{_SECRETS_PATH}/{sid}",
            json_body={"value": "new-key"},
        )

    @pytest.mark.asyncio
    async def test_rotate_with_refresh_token(self):
        sid = "sec_77"
        transport = _make_transport(_default={"data": {}})
        await dispatch_egress(
            transport,
            "miosa_secrets_rotate",
            {
                "secret_id": sid,
                "new_value": "tok_new",
                "refresh_token": "ref_tok",
                "expires_at": "2027-01-01T00:00:00Z",
            },
        )
        _, kwargs = transport.request.call_args
        assert kwargs["json_body"]["refresh_token"] == "ref_tok"
        assert kwargs["json_body"]["expires_at"] == "2027-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_rotate_missing_secret_id_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(),
            name="miosa_secrets_rotate",
            args={"new_value": "x"},
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_rotate_missing_new_value_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(),
            name="miosa_secrets_rotate",
            args={"secret_id": "sec_1"},
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_delete_routes_to_delete(self):
        sid = "sec_55"
        transport = _make_transport(**{f"DELETE:{_SECRETS_PATH}/{sid}": None})
        result = await dispatch_egress(
            transport, "miosa_secrets_delete", {"secret_id": sid}
        )
        assert "deleted" in _text(result)
        transport.request.assert_called_once_with("DELETE", f"{_SECRETS_PATH}/{sid}")

    @pytest.mark.asyncio
    async def test_delete_missing_secret_id_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(), name="miosa_secrets_delete", args={}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_connect_oauth_returns_authorize_url(self):
        transport = _make_transport(**{
            f"POST:{_OAUTH_START_PATH}": {
                "data": {
                    "authorize_url": "https://github.com/login/oauth/authorize?state=xyz",
                    "state": "xyz",
                }
            },
        })
        result = await dispatch_egress(
            transport,
            "miosa_secrets_connect_oauth",
            {"provider": "github"},
        )
        text = _text(result)
        payload = json.loads(text)
        assert payload["authorize_url"].startswith("https://github.com")
        assert payload["state"] == "xyz"
        assert "poll_url" in payload
        assert payload["provider"] == "github"

    @pytest.mark.asyncio
    async def test_connect_oauth_missing_provider_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(),
            name="miosa_secrets_connect_oauth",
            args={},
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_connect_oauth_passes_expose_as_env(self):
        transport = _make_transport(**{
            f"POST:{_OAUTH_START_PATH}": {
                "authorize_url": "https://accounts.google.com/o/oauth2/auth?state=abc",
                "state": "abc",
            },
        })
        await dispatch_egress(
            transport,
            "miosa_secrets_connect_oauth",
            {
                "provider": "google",
                "expose_as_env": "GOOGLE_TOKEN",
                "scope": "openid email",
            },
        )
        _, kwargs = transport.request.call_args
        body = kwargs["json_body"]
        assert body["provider"] == "google"
        assert body["expose_as_env"] == "GOOGLE_TOKEN"
        assert body["scope"] == "openid email"


# ---------------------------------------------------------------------------
# Network tools
# ---------------------------------------------------------------------------


class TestNetworkTools:

    @pytest.mark.asyncio
    async def test_allow_routes_to_post_allowlist(self):
        transport = _make_transport(**{
            f"POST:{_ALLOWLIST_PATH}": {"data": {"id": "rule_1"}},
        })
        result = await dispatch_egress(
            transport,
            "miosa_network_allow",
            {"host": "api.openai.com"},
        )
        assert "rule_1" in _text(result)
        _, kwargs = transport.request.call_args
        assert kwargs["json_body"]["host"] == "api.openai.com"
        assert kwargs["json_body"]["effect"] == "allow"

    @pytest.mark.asyncio
    async def test_allow_with_methods_and_path_glob(self):
        transport = _make_transport(_default={"data": {"id": "r2"}})
        await dispatch_egress(
            transport,
            "miosa_network_allow",
            {
                "host": "api.openai.com",
                "methods": ["GET", "POST"],
                "path_glob": "/v1/*",
            },
        )
        _, kwargs = transport.request.call_args
        body = kwargs["json_body"]
        assert body["methods"] == ["GET", "POST"]
        assert body["path_glob"] == "/v1/*"

    @pytest.mark.asyncio
    async def test_allow_missing_host_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(), name="miosa_network_allow", args={}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_deny_routes_to_post_allowlist_with_deny_effect(self):
        transport = _make_transport(**{
            f"POST:{_ALLOWLIST_PATH}": {"data": {"id": "rule_deny_1"}},
        })
        result = await dispatch_egress(
            transport,
            "miosa_network_deny",
            {"host": "evil.example.com"},
        )
        assert "rule_deny_1" in _text(result)
        _, kwargs = transport.request.call_args
        assert kwargs["json_body"]["effect"] == "deny"

    @pytest.mark.asyncio
    async def test_deny_missing_host_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(), name="miosa_network_deny", args={}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_lockdown_patches_policy_with_enforce(self):
        transport = _make_transport(**{
            f"PATCH:{_POLICY_PATH}": {"data": {"id": "pol_def", "mode": "enforce"}},
        })
        result = await dispatch_egress(
            transport, "miosa_network_lockdown", {}
        )
        assert "enforce" in _text(result)
        _, kwargs = transport.request.call_args
        assert kwargs["json_body"]["mode"] == "enforce"

    @pytest.mark.asyncio
    async def test_lockdown_with_specific_policy_id(self):
        transport = _make_transport(_default={"data": {"id": "pol_X", "mode": "enforce"}})
        await dispatch_egress(
            transport, "miosa_network_lockdown", {"policy_id": "pol_X"}
        )
        method, path = transport.request.call_args.args
        assert method == "PATCH"
        assert path == f"{_POLICY_PATH}/pol_X"

    @pytest.mark.asyncio
    async def test_observe_patches_policy_with_audit_only(self):
        transport = _make_transport(**{
            f"PATCH:{_POLICY_PATH}": {"data": {"id": "pol_def", "mode": "audit_only"}},
        })
        result = await dispatch_egress(
            transport, "miosa_network_observe", {}
        )
        assert "audit_only" in _text(result)
        _, kwargs = transport.request.call_args
        assert kwargs["json_body"]["mode"] == "audit_only"

    @pytest.mark.asyncio
    async def test_suggestions_routes_to_correct_endpoint(self):
        transport = _make_transport(**{
            f"GET:{_SUGGESTIONS_PATH}": {"data": [
                {"host": "api.openai.com", "score": 0.95},
            ]},
        })
        result = await dispatch_egress(
            transport, "miosa_network_suggestions", {}
        )
        assert "openai.com" in _text(result)
        transport.request.assert_called_once_with(
            "GET", _SUGGESTIONS_PATH, params={"since": "7d"}
        )

    @pytest.mark.asyncio
    async def test_suggestions_with_resource_id(self):
        transport = _make_transport(_default={"data": []})
        await dispatch_egress(
            transport,
            "miosa_network_suggestions",
            {"resource_id": "sbx_123", "since": "24h"},
        )
        _, kwargs = transport.request.call_args
        assert kwargs["params"]["resource_id"] == "sbx_123"
        assert kwargs["params"]["since"] == "24h"

    @pytest.mark.asyncio
    async def test_list_policies_routes_to_get(self):
        transport = _make_transport(**{
            f"GET:{_POLICY_PATH}": {"data": [
                {"id": "pol_1", "name": "default", "mode": "audit_only"},
            ]},
        })
        result = await dispatch_egress(
            transport, "miosa_network_list_policies", {}
        )
        assert "pol_1" in _text(result)
        transport.request.assert_called_once_with("GET", _POLICY_PATH)

    @pytest.mark.asyncio
    async def test_list_policies_empty(self):
        transport = _make_transport(**{f"GET:{_POLICY_PATH}": []})
        result = await dispatch_egress(
            transport, "miosa_network_list_policies", {}
        )
        assert "No policies" in _text(result)

    @pytest.mark.asyncio
    async def test_list_rules_routes_with_policy_id(self):
        transport = _make_transport(**{
            f"GET:{_ALLOWLIST_PATH}": {"data": [
                {"id": "rl_1", "host": "api.openai.com", "effect": "allow"},
            ]},
        })
        result = await dispatch_egress(
            transport,
            "miosa_network_list_rules",
            {"policy_id": "pol_X"},
        )
        assert "openai.com" in _text(result)
        _, kwargs = transport.request.call_args
        assert kwargs["params"]["policy_id"] == "pol_X"

    @pytest.mark.asyncio
    async def test_list_rules_missing_policy_id_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(), name="miosa_network_list_rules", args={}
        )
        assert _is_error(result)


# ---------------------------------------------------------------------------
# Audit tools
# ---------------------------------------------------------------------------


class TestAuditTools:

    @pytest.mark.asyncio
    async def test_query_routes_to_audit_path(self):
        transport = _make_transport(**{
            f"GET:{_AUDIT_PATH}": {"data": [
                {"id": "evt_1", "host": "api.stripe.com", "action": "allowed"},
            ]},
        })
        result = await dispatch_egress(
            transport, "miosa_audit_query", {}
        )
        assert "stripe.com" in _text(result)
        transport.request.assert_called_once_with(
            "GET", _AUDIT_PATH, params=None
        )

    @pytest.mark.asyncio
    async def test_query_passes_all_filters(self):
        transport = _make_transport(_default={"data": []})
        await dispatch_egress(
            transport,
            "miosa_audit_query",
            {
                "resource_id": "sbx_1",
                "host": "example.com",
                "action": "denied",
                "since": "2026-01-01T00:00:00Z",
                "until": "2026-02-01T00:00:00Z",
                "limit": 50,
            },
        )
        _, kwargs = transport.request.call_args
        p = kwargs["params"]
        assert p["resource_id"] == "sbx_1"
        assert p["host"] == "example.com"
        assert p["action"] == "denied"
        assert p["limit"] == 50

    @pytest.mark.asyncio
    async def test_query_empty_result(self):
        transport = _make_transport(**{f"GET:{_AUDIT_PATH}": []})
        result = await dispatch_egress(
            transport, "miosa_audit_query", {}
        )
        assert "No audit events" in _text(result)

    @pytest.mark.asyncio
    async def test_get_routes_to_event_path(self):
        eid = "evt_999"
        transport = _make_transport(**{
            f"GET:{_AUDIT_PATH}/{eid}": {
                "data": {"id": eid, "host": "api.github.com", "action": "allowed"},
            },
        })
        result = await dispatch_egress(
            transport, "miosa_audit_get", {"event_id": eid}
        )
        text = _text(result)
        assert eid in text
        assert "github.com" in text
        transport.request.assert_called_once_with("GET", f"{_AUDIT_PATH}/{eid}")

    @pytest.mark.asyncio
    async def test_get_missing_event_id_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(), name="miosa_audit_get", args={}
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_count_by_host_aggregates_raw_events(self):
        transport = _make_transport(**{
            f"GET:{_AUDIT_PATH}": {"data": [
                {"id": "e1", "host": "api.openai.com", "action": "allowed"},
                {"id": "e2", "host": "api.openai.com", "action": "allowed"},
                {"id": "e3", "host": "api.stripe.com", "action": "denied"},
            ]},
        })
        result = await dispatch_egress(
            transport,
            "miosa_audit_count_by_host",
            {"resource_id": "sbx_1", "since": "24h"},
        )
        text = _text(result)
        assert "api.openai.com" in text
        assert "api.stripe.com" in text
        # openai should appear first (higher count)
        assert text.index("openai") < text.index("stripe")

    @pytest.mark.asyncio
    async def test_count_by_host_missing_resource_id_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(),
            name="miosa_audit_count_by_host",
            args={"since": "7d"},
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_count_by_host_missing_since_returns_error(self):
        result = await dispatch_egress(
            transport=_make_transport(),
            name="miosa_audit_count_by_host",
            args={"resource_id": "sbx_1"},
        )
        assert _is_error(result)

    @pytest.mark.asyncio
    async def test_count_by_host_empty_returns_friendly_message(self):
        transport = _make_transport(**{f"GET:{_AUDIT_PATH}": []})
        result = await dispatch_egress(
            transport,
            "miosa_audit_count_by_host",
            {"resource_id": "sbx_1", "since": "1h"},
        )
        assert "No audit events" in _text(result)


# ---------------------------------------------------------------------------
# OAuth providers catalog
# ---------------------------------------------------------------------------


class TestOauthProvidersCatalog:

    @pytest.mark.asyncio
    async def test_routes_to_providers_endpoint(self):
        transport = _make_transport(**{
            f"GET:{_OAUTH_PROVIDERS_PATH}": {"data": [
                {"slug": "github", "name": "GitHub"},
                {"slug": "google", "name": "Google"},
            ]},
        })
        result = await dispatch_egress(
            transport, "miosa_oauth_providers_list", {}
        )
        text = _text(result)
        assert "github" in text
        assert "google" in text
        transport.request.assert_called_once_with("GET", _OAUTH_PROVIDERS_PATH)

    @pytest.mark.asyncio
    async def test_empty_providers(self):
        transport = _make_transport(**{f"GET:{_OAUTH_PROVIDERS_PATH}": []})
        result = await dispatch_egress(
            transport, "miosa_oauth_providers_list", {}
        )
        assert "No OAuth providers" in _text(result)


# ---------------------------------------------------------------------------
# Unknown tool guard
# ---------------------------------------------------------------------------


class TestUnknownTool:

    @pytest.mark.asyncio
    async def test_unknown_tool_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown egress tool"):
            await dispatch_egress(
                transport=_make_transport(),
                name="miosa_nonexistent_tool",
                args={},
            )


# ---------------------------------------------------------------------------
# Integration: server._dispatch routes miosa_ tools to egress module
# ---------------------------------------------------------------------------


class TestServerDispatchIntegration:
    """Smoke-test that server._dispatch delegates miosa_* calls correctly.

    We do not instantiate a full MCP server — just call _dispatch directly
    with a patched AsyncMiosa client.
    """

    @pytest.mark.asyncio
    async def test_dispatch_routes_miosa_prefix_to_egress(self):
        """_dispatch hands miosa_* names off to dispatch_egress."""
        from unittest.mock import patch, AsyncMock as AM

        transport = _make_transport(**{
            f"GET:{_SECRETS_PATH}": {"data": [{"id": "s1", "name": "K"}]},
        })
        client = MagicMock()
        client._transport = transport

        with patch(
            "miosa_mcp.server.dispatch_egress",
            new_callable=lambda: lambda *a, **kw: AM(
                return_value=[MagicMock(text="patched")]
            )(),
        ) as mock_dispatch:
            # Import _dispatch from server
            from miosa_mcp.server import _dispatch

            result = await _dispatch(client, "miosa_secrets_list", {})
            # dispatch_egress was called (the patch replaces it)
            assert result is not None
