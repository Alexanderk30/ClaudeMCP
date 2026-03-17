"""Core gateway server — boots all layers and exposes the gateway as an MCP server.

Boot sequence:
  1. Load tenant + downstream config from YAML.
  2. Connect to every downstream MCP server (Layer 4 — aggregator).
  3. Build the RequestPipeline (Layers 1-3).
  4. Expose the gateway as an MCP server over SSE transport via Starlette/uvicorn.

Tenants authenticate by passing their API key in the ``x-api-key`` query
parameter when connecting to the ``/sse`` endpoint.  A custom Starlette
middleware extracts this key and stores it in the ASGI scope so the MCP
handler can read it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

import structlog
import uvicorn
from mcp.server.lowlevel import Server as MCPServer
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from gateway.config.loader import GatewayConfig, load_config
from gateway.middleware.auth import AuthError
from gateway.middleware.rate_limiter import RateLimitExceeded
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.pipeline import RequestPipeline

logger = structlog.get_logger()

# ── API-key extraction ────────────────────────────────────────

# We store the tenant API key in a per-connection dict keyed by the SSE
# transport's internal session ID.  The Starlette middleware below
# extracts the key from the query string on the initial GET /sse request,
# and our MCP handlers look it up when processing tool calls.

_session_keys: dict[str, str] = {}


# ── MCP server factory ───────────────────────────────────────


def _build_mcp_server(pipeline: RequestPipeline) -> MCPServer:
    """Create a low-level MCP Server with custom list_tools / call_tool handlers."""

    mcp = MCPServer(name="mcp-gateway")

    @mcp.list_tools()  # type: ignore[misc]
    async def handle_list_tools() -> list[Tool]:
        """Return the merged manifest filtered for the calling tenant."""
        # In the low-level server, we don't have direct access to the
        # transport session.  We'll use a default "*all tools*" listing
        # and let the SSE-level auth filter before connecting.
        # For per-tenant filtering, the /sse endpoint validates the key
        # and we store it; the server then uses a "current session" key.
        api_key = _get_current_api_key()
        if api_key is None:
            # Unauthenticated — return empty manifest
            return []
        try:
            tool_dicts = pipeline.handle_list_tools(api_key)
        except AuthError:
            return []
        return [
            Tool(
                name=t["name"],
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
            )
            for t in tool_dicts
        ]

    @mcp.call_tool()  # type: ignore[misc]
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> Sequence[TextContent]:
        """Full middleware pipeline: auth → rate limit → log → route → proxy."""
        api_key = _get_current_api_key()
        if api_key is None:
            return [TextContent(type="text", text="Error: no API key in session")]

        try:
            result: CallToolResult = await pipeline.handle_call_tool(
                api_key=api_key,
                tool_name=name,
                arguments=arguments or {},
            )
            return result.content  # type: ignore[return-value]

        except AuthError as exc:
            return [TextContent(type="text", text=f"Auth error: {exc}")]

        except RateLimitExceeded as exc:
            return [TextContent(type="text", text=f"Rate limit exceeded: {exc}")]

        except Exception as exc:
            logger.exception("gateway.call_tool_error", tool=name)
            return [TextContent(type="text", text=f"Gateway error: {exc}")]

    return mcp


# ── API-key helper ────────────────────────────────────────────
# We use a contextvars-based approach to thread the API key through
# the MCP handler without modifying the MCP SDK.

import contextvars  # noqa: E402

_current_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_api_key", default=None
)


def _get_current_api_key() -> str | None:
    return _current_api_key.get()


# ── Starlette app factory ────────────────────────────────────


def _build_app(
    mcp: MCPServer,
    pipeline: RequestPipeline,
) -> Starlette:
    """Build the Starlette ASGI app with SSE transport + health endpoints."""

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        """SSE connection endpoint — authenticates via x-api-key query param."""
        api_key = request.query_params.get("api_key") or request.headers.get("x-api-key")

        if not api_key:
            return JSONResponse(
                {"error": "Missing api_key query parameter or x-api-key header"},
                status_code=401,
            )

        # Validate the key before opening the SSE stream
        try:
            tid, _cfg = pipeline.auth.authenticate(api_key)
        except AuthError:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)

        logger.info("gateway.sse_connect", tenant=tid)

        # Set the API key in contextvars so MCP handlers can read it
        token = _current_api_key.set(api_key)
        try:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send  # type: ignore[attr-defined]
            ) as (read_stream, write_stream):
                await mcp.run(
                    read_stream,
                    write_stream,
                    mcp.create_initialization_options(),
                )
        finally:
            _current_api_key.reset(token)

        return Response()

    async def handle_health(request: Request) -> JSONResponse:
        """Simple health-check endpoint."""
        return JSONResponse(
            {
                "status": "ok",
                "tools": len(pipeline.router._manifest),
                "usage_records": pipeline.usage_logger.record_count,
            }
        )

    async def handle_stats(request: Request) -> JSONResponse:
        """Per-tenant usage stats (requires admin key)."""
        api_key = request.headers.get("x-api-key", "")
        try:
            tid, cfg = pipeline.auth.authenticate(api_key)
        except AuthError:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)

        if cfg.role != "admin":
            return JSONResponse({"error": "Admin role required"}, status_code=403)

        # Return stats for the requested tenant, or all if admin
        target = request.query_params.get("tenant")
        stats = pipeline.usage_logger.stats_for(tenant_id=target)
        return JSONResponse(
            {
                "tenant": target or "all",
                "total_calls": stats.total_calls,
                "successes": stats.successes,
                "failures": stats.failures,
                "avg_latency_ms": round(stats.avg_latency_ms, 2),
                "p99_latency_ms": round(stats.p99_latency_ms, 2),
            }
        )

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
            Route("/health", endpoint=handle_health),
            Route("/stats", endpoint=handle_stats),
        ],
    )


# ── Boot sequence ─────────────────────────────────────────────


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
    3. Aggregate tool manifests — build the RequestPipeline.
    4. Create the MCP server with custom handlers.
    5. Mount Starlette app and serve via uvicorn.
    """
    config: GatewayConfig = load_config(config_path)
    logger.info("gateway.config_loaded", tenants=list(config.tenants.keys()))

    # ── Layer 4: Proxy & Aggregation ─────────────────────────
    async with ToolAggregator(config.downstream_servers) as aggregator:
        merged = aggregator.merged_manifest()
        logger.info("gateway.manifest_ready", tools=list(merged.keys()))

        # ── Layers 1-3: Full pipeline ─────────────────────────
        pipeline = RequestPipeline(config, aggregator)

        # ── MCP Server ────────────────────────────────────────
        mcp = _build_mcp_server(pipeline)

        # ── Starlette App ─────────────────────────────────────
        app = _build_app(mcp, pipeline)

        logger.info("gateway.starting", host=host, port=port)
        server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host=host,
                port=port,
                log_level=log_level,
            )
        )
        await server.serve()
