"""Tests for the RequestPipeline (Layer 3 orchestration)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from gateway.config.loader import GatewayConfig, StdioTransport, TenantConfig
from gateway.middleware.auth import AuthError
from gateway.middleware.rate_limiter import RateLimitExceeded
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.pipeline import RequestPipeline


# ── Helpers ───────────────────────────────────────────────────


def _make_config() -> GatewayConfig:
    return GatewayConfig(
        tenants={
            "admin": TenantConfig(
                api_key="sk-admin",
                role="admin",
                allowed_tools=["*"],
                rate_limit=100,
                downstream=["server_a", "server_b"],
            ),
            "viewer": TenantConfig(
                api_key="sk-viewer",
                role="viewer",
                allowed_tools=["server_a:read"],
                rate_limit=2,
                downstream=["server_a"],
            ),
        },
        downstream_servers={
            "server_a": StdioTransport(command="mcp-a"),
            "server_b": StdioTransport(command="mcp-b"),
        },
    )


def _mock_tool(name: str, desc: str = "") -> Tool:
    return Tool(name=name, description=desc, inputSchema={"type": "object", "properties": {}})


def _mock_call_result(text: str = "ok") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


@asynccontextmanager
async def _noop_transport(*_a: Any, **_kw: Any):
    yield (MagicMock(), MagicMock())


def _mock_session(tools: list[Tool], call_result: CallToolResult | None = None) -> AsyncMock:
    session = AsyncMock()
    list_result = MagicMock()
    list_result.tools = tools
    list_result.nextCursor = None
    session.list_tools.return_value = list_result
    session.call_tool.return_value = call_result or _mock_call_result()
    init_result = MagicMock()
    init_result.serverInfo = MagicMock(name="mock")
    init_result.protocolVersion = "2025-06-18"
    session.initialize.return_value = init_result
    return session


async def _create_pipeline() -> tuple[RequestPipeline, ToolAggregator]:
    """Build a pipeline with mocked downstream servers."""
    config = _make_config()
    sessions = {
        "server_a": _mock_session(
            [_mock_tool("read", "Read stuff"), _mock_tool("write", "Write stuff")],
            call_result=_mock_call_result("from-a"),
        ),
        "server_b": _mock_session(
            [_mock_tool("deploy", "Deploy stuff")],
            call_result=_mock_call_result("from-b"),
        ),
    }
    session_iter = iter(sessions.values())

    with (
        patch("gateway.proxy.aggregator.stdio_client", _noop_transport),
        patch("gateway.proxy.aggregator.sse_client", _noop_transport),
        patch("gateway.proxy.aggregator.ClientSession", side_effect=lambda *a, **k: next(session_iter)),
    ):
        aggregator = ToolAggregator(config.downstream_servers)
        await aggregator.__aenter__()

    pipeline = RequestPipeline(config, aggregator)
    return pipeline, aggregator


# ── handle_list_tools ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tools_admin_sees_all() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        tools = pipeline.handle_list_tools("sk-admin")
        names = {t["name"] for t in tools}
        # Admin has downstream=[server_a, server_b], wildcard tools
        assert "server_a:read" in names
        assert "server_a:write" in names
        assert "server_b:deploy" in names
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_list_tools_viewer_sees_restricted() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        tools = pipeline.handle_list_tools("sk-viewer")
        names = {t["name"] for t in tools}
        # Viewer: allowed_tools=["server_a:read"], downstream=["server_a"]
        assert names == {"server_a:read"}
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_list_tools_bad_key_raises() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        with pytest.raises(AuthError):
            pipeline.handle_list_tools("bad-key")
    finally:
        await agg.disconnect_all()


# ── handle_call_tool ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_success() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        result = await pipeline.handle_call_tool(
            api_key="sk-admin",
            tool_name="server_a:read",
            arguments={"path": "/"},
        )
        assert result.content[0].text == "from-a"  # type: ignore[union-attr]
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_call_tool_auth_denied() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        with pytest.raises(AuthError):
            await pipeline.handle_call_tool(
                api_key="sk-viewer",
                tool_name="server_a:write",  # viewer can only read
                arguments={},
            )
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_call_tool_downstream_denied() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        # viewer's allowed_tools=["server_a:read"] blocks this at the tool ACL
        # level before the downstream check even fires
        with pytest.raises(AuthError):
            await pipeline.handle_call_tool(
                api_key="sk-viewer",
                tool_name="server_b:deploy",  # viewer has no access to server_b
                arguments={},
            )
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_call_tool_rate_limited() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        # Viewer has rate_limit=2
        await pipeline.handle_call_tool("sk-viewer", "server_a:read", {})
        await pipeline.handle_call_tool("sk-viewer", "server_a:read", {})
        with pytest.raises(RateLimitExceeded):
            await pipeline.handle_call_tool("sk-viewer", "server_a:read", {})
    finally:
        await agg.disconnect_all()


@pytest.mark.asyncio
async def test_call_tool_logs_usage() -> None:
    pipeline, agg = await _create_pipeline()
    try:
        await pipeline.handle_call_tool("sk-admin", "server_a:read", {})
        records = pipeline.usage_logger.query(tenant_id="admin")
        assert len(records) == 1
        assert records[0].tool_name == "server_a:read"
        assert records[0].success is True
    finally:
        await agg.disconnect_all()


# ── effective_patterns ────────────────────────────────────────


def test_effective_patterns_wildcard_scoped_to_downstream() -> None:
    """Wildcard + downstream list → scoped patterns."""
    tenant = TenantConfig(
        api_key="x", role="admin", allowed_tools=["*"],
        rate_limit=10, downstream=["fs", "gh"],
    )
    patterns = RequestPipeline._effective_patterns(tenant)
    assert set(patterns) == {"fs:*", "gh:*"}


def test_effective_patterns_explicit_tools_unchanged() -> None:
    """Non-wildcard tools pass through as-is."""
    tenant = TenantConfig(
        api_key="x", role="viewer", allowed_tools=["fs:read"],
        rate_limit=10, downstream=["fs"],
    )
    patterns = RequestPipeline._effective_patterns(tenant)
    assert patterns == ["fs:read"]
