"""Tests for the auth middleware (Layer 1)."""

import pytest

from gateway.config.loader import TenantConfig
from gateway.middleware.auth import AuthError, AuthMiddleware

_TENANTS = {
    "acme": TenantConfig(
        api_key="sk-acme",
        role="admin",
        allowed_tools=["*"],
        rate_limit=100,
        downstream=["filesystem", "github"],
    ),
    "viewer": TenantConfig(
        api_key="sk-viewer",
        role="viewer",
        allowed_tools=["filesystem:read_file"],
        rate_limit=10,
        downstream=["filesystem"],
    ),
    "editor": TenantConfig(
        api_key="sk-editor",
        role="editor",
        allowed_tools=["filesystem:*", "github:create_issue"],
        rate_limit=60,
        downstream=["filesystem", "github"],
    ),
}


# ── authenticate ──────────────────────────────────────────────


def test_authenticate_valid_key() -> None:
    auth = AuthMiddleware(_TENANTS)
    tid, cfg = auth.authenticate("sk-acme")
    assert tid == "acme"
    assert cfg.role == "admin"


def test_authenticate_invalid_key() -> None:
    auth = AuthMiddleware(_TENANTS)
    with pytest.raises(AuthError, match="Invalid API key"):
        auth.authenticate("sk-bogus")


def test_authenticate_timing_safe() -> None:
    """authenticate uses hmac.compare_digest — just verify it works."""
    auth = AuthMiddleware(_TENANTS)
    # Near-miss key should still fail
    with pytest.raises(AuthError):
        auth.authenticate("sk-acm")  # one char short


# ── authorize (tool-level ACL) ────────────────────────────────


def test_authorize_wildcard_within_downstream() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-acme")
    # acme has wildcard tools + downstream=[filesystem, github]
    auth.authorize(cfg, "filesystem:read_file")
    auth.authorize(cfg, "github:create_issue")


def test_authorize_wildcard_blocked_by_downstream() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-acme")
    # acme has wildcard tools but downstream doesn't include "unknown"
    with pytest.raises(AuthError, match="does not have access to downstream"):
        auth.authorize(cfg, "unknown:something")


def test_authorize_restricted_tool_allowed() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-viewer")
    auth.authorize(cfg, "filesystem:read_file")  # explicitly allowed


def test_authorize_restricted_tool_denied() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-viewer")
    with pytest.raises(AuthError, match="not authorized for tool"):
        auth.authorize(cfg, "filesystem:write_file")


def test_authorize_glob_pattern() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-editor")
    auth.authorize(cfg, "filesystem:read_file")  # matches filesystem:*
    auth.authorize(cfg, "filesystem:write_file")  # matches filesystem:*
    auth.authorize(cfg, "github:create_issue")  # exact match


def test_authorize_glob_pattern_denied() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-editor")
    with pytest.raises(AuthError):
        auth.authorize(cfg, "github:list_repos")  # not in allowed_tools


# ── authorize (downstream-level ACL) ─────────────────────────


def test_authorize_downstream_denied() -> None:
    """A tenant with wildcard tools but limited downstream gets blocked."""
    # Create a tenant with wildcard tools but only filesystem downstream
    tenants = {
        "wide_tools": TenantConfig(
            api_key="sk-wide", role="editor", allowed_tools=["*"],
            rate_limit=10, downstream=["filesystem"],
        ),
    }
    auth = AuthMiddleware(tenants)
    _, cfg = auth.authenticate("sk-wide")
    with pytest.raises(AuthError, match="does not have access to downstream"):
        auth.authorize(cfg, "github:list_repos")


def test_authorize_downstream_allowed() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-editor")
    # editor has filesystem + github
    auth.authorize(cfg, "github:create_issue")


# ── authorize_full ────────────────────────────────────────────


def test_authorize_full_success() -> None:
    auth = AuthMiddleware(_TENANTS)
    tid, cfg = auth.authorize_full("sk-editor", "filesystem:read_file")
    assert tid == "editor"


def test_authorize_full_bad_key() -> None:
    auth = AuthMiddleware(_TENANTS)
    with pytest.raises(AuthError, match="Invalid API key"):
        auth.authorize_full("bad-key", "filesystem:read_file")


def test_authorize_full_bad_tool() -> None:
    auth = AuthMiddleware(_TENANTS)
    with pytest.raises(AuthError):
        auth.authorize_full("sk-viewer", "github:create_issue")


# ── reload ────────────────────────────────────────────────────


def test_reload_adds_new_tenant() -> None:
    auth = AuthMiddleware(_TENANTS)
    with pytest.raises(AuthError):
        auth.authenticate("sk-new")

    new_tenants = {
        **_TENANTS,
        "new_tenant": TenantConfig(
            api_key="sk-new", role="viewer", allowed_tools=["*"],
            rate_limit=10, downstream=[],
        ),
    }
    auth.reload(new_tenants)
    tid, _ = auth.authenticate("sk-new")
    assert tid == "new_tenant"
