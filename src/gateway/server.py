"""Core gateway server.

Boots the middleware pipeline, connects to downstream MCP servers, and
exposes everything as an SSE-based MCP server via Starlette + uvicorn.

Tenants auth by passing their key as the `api_key` query param (or
`x-api-key` header) when hitting /sse.
"""

from __future__ import annotations

import contextvars
from typing import Any, Sequence

import structlog
import uvicorn
from mcp.server.lowlevel import Server as MCPServer
from mcp.server.sse import SseServerTransport
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from gateway.config.loader import GatewayConfig, load_config
from gateway.middleware.auth import AuthError
from gateway.middleware.rate_limiter import RateLimitExceeded
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.pipeline import RequestPipeline

logger = structlog.get_logger()

# Per-connection API key, threaded through via contextvars so the MCP
# handlers can access it without us patching the SDK.
_current_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_api_key", default=None,
)


def _get_current_api_key() -> str | None:
    return _current_api_key.get()


def _build_mcp_server(pipeline: RequestPipeline) -> MCPServer:
    """Wire up list_tools / call_tool on a low-level MCP Server."""

    mcp = MCPServer(name="mcp-gateway")

    @mcp.list_tools()  # type: ignore[misc]
    async def handle_list_tools() -> list[Tool]:
        api_key = _get_current_api_key()
        if api_key is None:
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
        """Run the full middleware pipeline for a single tool call."""
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


def _build_app(mcp: MCPServer, pipeline: RequestPipeline) -> Starlette:
    """Starlette ASGI app with SSE transport + health/stats endpoints."""

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        api_key = request.query_params.get("api_key") or request.headers.get("x-api-key")
        if not api_key:
            return JSONResponse(
                {"error": "Missing api_key query parameter or x-api-key header"},
                status_code=401,
            )

        try:
            tid, _cfg = pipeline.auth.authenticate(api_key)
        except AuthError:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)

        logger.info("gateway.sse_connect", tenant=tid)

        token = _current_api_key.set(api_key)
        try:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send  # type: ignore[attr-defined]
            ) as (read_stream, write_stream):
                await mcp.run(
                    read_stream, write_stream,
                    mcp.create_initialization_options(),
                )
        finally:
            _current_api_key.reset(token)

        return Response()

    async def handle_health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "tools": len(pipeline.router._manifest),
            "usage_records": pipeline.usage_logger.record_count,
        })

    async def handle_stats(request: Request) -> JSONResponse:
        """Per-tenant usage stats. Requires admin role."""
        api_key = request.headers.get("x-api-key", "")
        try:
            tid, cfg = pipeline.auth.authenticate(api_key)
        except AuthError:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)

        if cfg.role != "admin":
            return JSONResponse({"error": "Admin role required"}, status_code=403)

        target = request.query_params.get("tenant")
        stats = pipeline.usage_logger.stats_for(tenant_id=target)
        return JSONResponse({
            "tenant": target or "all",
            "total_calls": stats.total_calls,
            "successes": stats.successes,
            "failures": stats.failures,
            "avg_latency_ms": round(stats.avg_latency_ms, 2),
            "p99_latency_ms": round(stats.p99_latency_ms, 2),
        })

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
            Route("/health", endpoint=handle_health),
            Route("/stats", endpoint=handle_stats),
        ],
    )


async def run_gateway(
    config_path: str,
    host: str = "0.0.0.0",
    port: int = 8000,
    log_level: str = "info",
) -> None:
    """Load config, spin up downstream connections, and serve."""
    config: GatewayConfig = load_config(config_path)
    logger.info("gateway.config_loaded", tenants=list(config.tenants.keys()))

    async with ToolAggregator(config.downstream_servers) as aggregator:
        merged = aggregator.merged_manifest()
        logger.info("gateway.manifest_ready", tools=list(merged.keys()))

        pipeline = RequestPipeline(config, aggregator)
        mcp = _build_mcp_server(pipeline)
        app = _build_app(mcp, pipeline)

        logger.info("gateway.starting", host=host, port=port)
        server = uvicorn.Server(
            uvicorn.Config(app=app, host=host, port=port, log_level=log_level)
        )
        await server.serve()
