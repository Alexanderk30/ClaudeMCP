"""Request pipeline — orchestrates the full middleware chain.

Every inbound MCP tool call flows through:

    authenticate → authorize → rate-limit → usage-log → route → proxy

This module owns that composition so the server layer only needs to call
``pipeline.handle_list_tools()`` and ``pipeline.handle_call_tool()``.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.types import CallToolResult

from gateway.config.loader import GatewayConfig, TenantConfig
from gateway.middleware.auth import AuthError, AuthMiddleware
from gateway.middleware.rate_limiter import RateLimiter, RateStatus
from gateway.middleware.usage_logger import UsageLogger
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.router import ToolRouter

logger = structlog.get_logger()


class RequestPipeline:
    """Single entry point that wires auth → rate-limit → log → router → proxy."""

    def __init__(
        self,
        config: GatewayConfig,
        aggregator: ToolAggregator,
    ) -> None:
        self.auth = AuthMiddleware(config.tenants)
        self.rate_limiter = RateLimiter(config.tenants)
        self.usage_logger = UsageLogger()
        self.router = ToolRouter(
            manifest=aggregator.merged_manifest(),
            aggregator=aggregator,
        )
        self._aggregator = aggregator

    # ── tool listing ──────────────────────────────────────

    def handle_list_tools(self, api_key: str) -> list[dict[str, Any]]:
        """Return the tool manifest filtered for this tenant.

        Auth is checked but rate-limiting is *not* consumed for
        list operations (read-only introspection).
        """
        _tid, tenant = self.auth.authenticate(api_key)

        # Filter by both allowed_tools globs *and* allowed downstreams
        allowed = self._effective_patterns(tenant)
        return self.router.list_tools(allowed_patterns=allowed)

    # ── tool calling ──────────────────────────────────────

    async def handle_call_tool(
        self,
        api_key: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        """Full middleware chain for a single tool call."""
        # 1. Auth
        tid, tenant = self.auth.authenticate(api_key)
        self.auth.authorize(tenant, tool_name)

        # 2. Rate limit
        status: RateStatus = self.rate_limiter.check(tid)
        logger.debug(
            "pipeline.rate_status",
            tenant=tid,
            remaining=status.remaining,
        )

        # 3. Usage logging wraps the actual call
        async with self.usage_logger.track(tid, tool_name):
            result = await self.router.call_tool(tool_name, arguments)

        return result

    # ── hot-reload ────────────────────────────────────────

    def reload_config(self, config: GatewayConfig) -> None:
        """Hot-reload tenant configs across all middleware."""
        self.auth.reload(config.tenants)
        self.rate_limiter.reload(config.tenants)
        logger.info("pipeline.reloaded")

    async def refresh_tools(self) -> None:
        """Re-fetch downstream tool manifests and rebuild the router."""
        await self._aggregator.refresh_manifest()
        self.router.reload_manifest()
        logger.info("pipeline.tools_refreshed")

    # ── helpers ────────────────────────────────────────────

    @staticmethod
    def _effective_patterns(tenant: TenantConfig) -> list[str]:
        """Merge allowed_tools with downstream server restrictions.

        If a tenant has ``allowed_tools: ["*"]`` but ``downstream: [filesystem]``,
        the effective patterns become ``["filesystem:*"]``.
        """
        patterns = tenant.allowed_tools

        if not tenant.downstream:
            return patterns

        # If wildcard, scope it to allowed downstreams
        if patterns == ["*"]:
            return [f"{ds}:*" for ds in tenant.downstream]

        return patterns
