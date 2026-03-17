"""Layer 1 — Authentication & permissions middleware.

Responsibilities:
  - Validate the inbound API key against the tenant registry
    (timing-safe comparison to prevent side-channel leaks).
  - Resolve the tenant identity, role, and allowed downstream servers.
  - Check whether the requested tool passes the tenant's allowed_tools
    glob list *and* belongs to a downstream the tenant may reach.
"""

from __future__ import annotations

import fnmatch
import hmac

import structlog

from gateway.config.loader import TenantConfig

logger = structlog.get_logger()


class AuthError(Exception):
    """Raised when authentication or authorization fails."""


class AuthMiddleware:
    """Stateless auth gate — call .authenticate() then .authorize()."""

    def __init__(self, tenants: dict[str, TenantConfig]) -> None:
        self._tenants = dict(tenants)

    # ── public API ────────────────────────────────────────

    def authenticate(self, api_key: str) -> tuple[str, TenantConfig]:
        """Return *(tenant_id, config)* or raise :class:`AuthError`.

        Uses constant-time comparison so an attacker cannot infer valid
        key prefixes from timing differences.
        """
        for tid, cfg in self._tenants.items():
            if hmac.compare_digest(api_key, cfg.api_key):
                return tid, cfg
        raise AuthError("Invalid API key")

    def authorize(self, tenant: TenantConfig, tool_name: str) -> None:
        """Raise :class:`AuthError` if the tenant may not call *tool_name*.

        Checks two things:
        1. The tool's qualified name matches at least one allowed_tools glob.
        2. The tool belongs to a downstream server the tenant has access to.
        """
        # ── tool-level ACL ────────────────────────────────
        if not self._matches_any(tool_name, tenant.allowed_tools):
            raise AuthError(f"Tenant not authorized for tool '{tool_name}'")

        # ── downstream-level ACL ──────────────────────────
        if tenant.downstream:
            server_name = tool_name.split(":")[0] if ":" in tool_name else ""
            if server_name and server_name not in tenant.downstream:
                raise AuthError(
                    f"Tenant does not have access to downstream server '{server_name}'"
                )

    def authorize_full(
        self, api_key: str, tool_name: str
    ) -> tuple[str, TenantConfig]:
        """Convenience: authenticate + authorize in one call."""
        tid, cfg = self.authenticate(api_key)
        self.authorize(cfg, tool_name)
        return tid, cfg

    # ── hot-reload ────────────────────────────────────────

    def reload(self, tenants: dict[str, TenantConfig]) -> None:
        """Replace the tenant registry (e.g. after config file change)."""
        self._tenants = dict(tenants)
        logger.info("auth.reloaded", tenant_count=len(self._tenants))

    # ── internals ─────────────────────────────────────────

    @staticmethod
    def _matches_any(value: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(value, p) for p in patterns)
