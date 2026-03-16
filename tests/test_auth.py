"""Tests for the auth middleware."""

import pytest

from gateway.config.loader import TenantConfig
from gateway.middleware.auth import AuthError, AuthMiddleware

_TENANTS = {
    "acme": TenantConfig(
        api_key="sk-acme",
        role="admin",
        allowed_tools=["*"],
        rate_limit=100,
        downstream=["filesystem"],
    ),
    "viewer": TenantConfig(
        api_key="sk-viewer",
        role="viewer",
        allowed_tools=["filesystem:read_file"],
        rate_limit=10,
        downstream=["filesystem"],
    ),
}


def test_authenticate_valid_key() -> None:
    auth = AuthMiddleware(_TENANTS)
    tid, cfg = auth.authenticate("sk-acme")
    assert tid == "acme"
    assert cfg.role == "admin"


def test_authenticate_invalid_key() -> None:
    auth = AuthMiddleware(_TENANTS)
    with pytest.raises(AuthError):
        auth.authenticate("sk-bogus")


def test_authorize_wildcard() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-acme")
    auth.authorize(cfg, "anything:goes")  # should not raise


def test_authorize_restricted() -> None:
    auth = AuthMiddleware(_TENANTS)
    _, cfg = auth.authenticate("sk-viewer")
    auth.authorize(cfg, "filesystem:read_file")  # allowed
    with pytest.raises(AuthError):
        auth.authorize(cfg, "filesystem:write_file")  # denied
