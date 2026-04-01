"""Authentication and authorization middleware.

Validates inbound API keys (timing-safe), resolves tenant identity,
and checks tool-level + downstream-level access.
"""

from __future__ import annotations

import fnmatch
import hmac

import structlog

from gateway.config.loader import TenantConfig

logger = structlog.get_logger()


class AuthError(Exception):
    """Authentication or authorization failure."""


class AuthMiddleware:
    """Stateless auth gate: authenticate() then authorize()."""

    def __init__(self, tenants: dict[str, TenantConfig]) -> None:
        self._tenants = dict(tenants)

    def authenticate(self, api_key: str) -> tuple[str, TenantConfig]:
        """Return (tenant_id, config) or raise AuthError.

        Uses constant-time comparison to avoid timing side-channels.
        """
        for tid, cfg in self._tenants.items():
            if hmac.compare_digest(api_key, cfg.api_key):
                return tid, cfg
        raise AuthError("Invalid API key")

    def authorize(self, tenant: TenantConfig, tool_name: str) -> None:
        """Check tool-level ACL and downstream-level ACL."""
        if not self._matches_any(tool_name, tenant.allowed_tools):
            raise AuthError(f"Tenant not authorized for tool '{tool_name}'")

        if tenant.downstream:
            server = tool_name.split(":")[0] if ":" in tool_name else ""
            if server and server not in tenant.downstream:
                raise AuthError(
                    f"Tenant does not have access to downstream server '{server}'"
                )

    def authorize_full(
        self, api_key: str, tool_name: str,
    ) -> tuple[str, TenantConfig]:
        """authenticate() + authorize() in one shot."""
        tid, cfg = self.authenticate(api_key)
        self.authorize(cfg, tool_name)
        return tid, cfg

    def reload(self, tenants: dict[str, TenantConfig]) -> None:
        """Swap in a new tenant registry (e.g. after config change)."""
        self._tenants = dict(tenants)
        logger.info("auth.reloaded", tenant_count=len(self._tenants))

    @staticmethod
    def _matches_any(value: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(value, p) for p in patterns)
