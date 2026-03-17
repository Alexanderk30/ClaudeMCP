"""Tests for the server layer (SSE + Starlette endpoints)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool
from starlette.testclient import TestClient

from gateway.config.loader import GatewayConfig, StdioTransport, TenantConfig
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.pipeline import RequestPipeline
from gateway.server import _build_app, _build_mcp_server


# ── Helpers ───────────────────────────────────────────────────

from contextlib import asynccontextmanager


@asynccontextmanager
async def _noop_transport(*_a: Any, **_kw: Any):
    yield (MagicMock(), MagicMock())


def _mock_session(tools: list[Tool], call_result: CallToolResult | None = None) -> AsyncMock:
    session = AsyncMock()
    list_result = MagicMock()
    list_result.tools = tools
    list_result.nextCursor = None
    session.list_tools.return_value = list_result
    session.call_tool.return_value = call_result or CallToolResult(
        content=[TextContent(type="text", text="ok")]
    )
    init_result = MagicMock()
    init_result.serverInfo = MagicMock(name="mock")
    init_result.protocolVersion = "2025-06-18"
    session.initialize.return_value = init_result
    return session


def _make_config() -> GatewayConfig:
    return GatewayConfig(
        tenants={
            "admin": TenantConfig(
                api_key="sk-admin", role="admin",
                allowed_tools=["*"], rate_limit=100,
                downstream=["srv"],
            ),
            "viewer": TenantConfig(
                api_key="sk-viewer", role="viewer",
                allowed_tools=["srv:read"], rate_limit=10,
                downstream=["srv"],
            ),
        },
        downstream_servers={
            "srv": StdioTransport(command="mcp-srv"),
        },
    )


async def _create_app() -> tuple[Any, ToolAggregator]:
    config = _make_config()
    mock = _mock_session([
        Tool(name="read", description="Read", inputSchema={"type": "object", "properties": {}}),
        Tool(name="write", description="Write", inputSchema={"type": "object", "properties": {}}),
    ])
    with (
        patch("gateway.proxy.aggregator.stdio_client", _noop_transport),
        patch("gateway.proxy.aggregator.sse_client", _noop_transport),
        patch("gateway.proxy.aggregator.ClientSession", return_value=mock),
    ):
        aggregator = ToolAggregator(config.downstream_servers)
        await aggregator.__aenter__()

    pipeline = RequestPipeline(config, aggregator)
    mcp = _build_mcp_server(pipeline)
    app = _build_app(mcp, pipeline)
    return app, aggregator


# ── Health endpoint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["tools"] == 2
    finally:
        await agg.disconnect_all()


# ── Stats endpoint ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_requires_auth() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/stats")
        assert resp.status_code == 403
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_stats_requires_admin_role() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/stats", headers={"x-api-key": "sk-viewer"})
        assert resp.status_code == 403
        assert "Admin role required" in resp.json()["error"]
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_stats_admin_success() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/stats", headers={"x-api-key": "sk-admin"})
        assert resp.status_code == 200
        body = resp.json()
        assert "total_calls" in body
        assert "avg_latency_ms" in body
    finally:
        await agg.disconnect_all()


# ── SSE endpoint auth ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_rejects_no_key() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/sse")
        assert resp.status_code == 401
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_sse_rejects_bad_key() -> None:
    app, agg = await _create_app()
    try:
        client = TestClient(app)
        resp = client.get("/sse?api_key=bad-key")
        assert resp.status_code == 403
    finally:
        await agg.disconnect_all()
