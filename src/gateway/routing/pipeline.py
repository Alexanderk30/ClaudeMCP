"""Request pipeline — wires together the middleware chain.

Every tool call goes through: auth -> rate-limit -> usage-log -> route -> proxy.
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
    """Single entry point that composes auth, rate-limit, logging, and routing."""

    def __init__(self, config: GatewayConfig, aggregator: ToolAggregator) -> None:
        self.auth = AuthMiddleware(config.tenants)
        self.rate_limiter = RateLimiter(config.tenants)
        self.usage_logger = UsageLogger()
        self.router = ToolRouter(
            manifest=aggregator.merged_manifest(),
            aggregator=aggregator,
        )
        self._aggregator = aggregator

    def handle_list_tools(self, api_key: str) -> list[dict[str, Any]]:
        """Filtered tool manifest for a tenant. No rate-limit cost."""
        _tid, tenant = self.auth.authenticate(api_key)
        allowed = self._effective_patterns(tenant)
        return self.router.list_tools(allowed_patterns=allowed)

    async def handle_call_tool(
        self, api_key: str, tool_name: str, arguments: dict[str, Any],
    ) -> CallToolResult:
        """Full middleware chain for one tool call."""
        tid, tenant = self.auth.authenticate(api_key)
        self.auth.authorize(tenant, tool_name)

        status: RateStatus = self.rate_limiter.check(tid)
        logger.debug("pipeline.rate_status", tenant=tid, remaining=status.remaining)

        async with self.usage_logger.track(tid, tool_name):
            result = await self.router.call_tool(tool_name, arguments)
        return result

    def reload_config(self, config: GatewayConfig) -> None:
        self.auth.reload(config.tenants)
        self.rate_limiter.reload(config.tenants)
        logger.info("pipeline.reloaded")

    async def refresh_tools(self) -> None:
        """Re-fetch downstream manifests and rebuild the router."""
        await self._aggregator.refresh_manifest()
        self.router.reload_manifest()
        logger.info("pipeline.tools_refreshed")

    @staticmethod
    def _effective_patterns(tenant: TenantConfig) -> list[str]:
        """If a tenant has wildcard tools but restricted downstreams, scope it."""
        patterns = tenant.allowed_tools
        if not tenant.downstream:
            return patterns
        if patterns == ["*"]:
            return [f"{ds}:*" for ds in tenant.downstream]
        return patterns
