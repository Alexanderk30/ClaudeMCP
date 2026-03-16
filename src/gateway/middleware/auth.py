"""Layer 1 — Authentication & permissions middleware.

Responsibilities:
  - Validate the inbound API key against the tenant registry.
  - Resolve the tenant identity and role.
  - Check whether the requested tool is in the tenant's allowed_tools list.
"""

from __future__ import annotations

import fnmatch

from gateway.config.loader import TenantConfig


class AuthError(Exception):
    """Raised when authentication or authorization fails."""


class AuthMiddleware:
    """Stateless auth gate — call .authenticate() then .authorize()."""

    def __init__(self, tenants: dict[str, TenantConfig]) -> None:
        # Build a reverse lookup: api_key → (tenant_id, TenantConfig)
        self._key_map: dict[str, tuple[str, TenantConfig]] = {
            t.api_key: (tid, t) for tid, t in tenants.items()
        }

    # ── public API ───────────────────────────────────────────

    def authenticate(self, api_key: str) -> tuple[str, TenantConfig]:
        """Return (tenant_id, config) or raise AuthError."""
        result = self._key_map.get(api_key)
        if result is None:
            raise AuthError("Invalid API key")
        return result

    def authorize(self, tenant: TenantConfig, tool_name: str) -> None:
        """Raise AuthError if the tenant may not call *tool_name*."""
        for pattern in tenant.allowed_tools:
            if fnmatch.fnmatch(tool_name, pattern):
                return
        raise AuthError(f"Tenant not authorized for tool '{tool_name}'")
