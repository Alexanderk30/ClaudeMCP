"""Core gateway server — boots up the MCP SSE server and wires all middleware."""

from __future__ import annotations

import structlog

from gateway.config.loader import GatewayConfig, load_config
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.rate_limiter import RateLimiter
from gateway.middleware.usage_logger import UsageLogger
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.router import ToolRouter

logger = structlog.get_logger()


async def run_gateway(
    config_path: str,
    host: str = "0.0.0.0",
    port: int = 8000,
    log_level: str = "info",
) -> None:
    """
    High-level boot sequence:

    1. Load tenant + downstream config from YAML.
    2. Start downstream MCP client connections (stdio / SSE).
    3. Aggregate tool manifests from all downstream servers.
    4. Build the ToolRouter (tool-name → downstream mapping).
    5. Mount middleware stack: Auth → RateLimit → UsageLog → Router → Proxy.
    6. Expose the gateway itself as an MCP server (SSE transport).
    """
    config: GatewayConfig = load_config(config_path)
    logger.info("gateway.config_loaded", tenants=list(config.tenants.keys()))

    # ── Layer 4: Proxy & Aggregation ─────────────────────────
    aggregator = ToolAggregator(config.downstream_servers)
    await aggregator.connect_all()
    merged_manifest = aggregator.merged_manifest()

    # ── Layer 3: Tool Router ─────────────────────────────────
    router = ToolRouter(manifest=merged_manifest, aggregator=aggregator)

    # ── Layers 1 & 2: Middleware ─────────────────────────────
    auth = AuthMiddleware(config.tenants)
    rate_limiter = RateLimiter(config.tenants)
    usage_logger = UsageLogger()

    # ── MCP Server (SSE transport) ───────────────────────────
    # TODO: wire up the actual MCP server with the middleware pipeline
    logger.info("gateway.starting", host=host, port=port)
    raise NotImplementedError("Server boot will be implemented in the next iteration.")
