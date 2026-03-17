"""Tests for Layer 4 — ToolAggregator & DownstreamConnection.

We mock the MCP transport + session layer so the tests run without any
real downstream servers.  The mocks verify that:

- Connections are opened via the correct transport (stdio / SSE)
- Tool manifests are fetched and merged under qualified names
- Duplicate tool names across servers are handled (first-wins)
- Tool calls are proxied to the correct downstream session
- Failed connections are logged but don't crash the aggregator
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from gateway.config.loader import SSETransport, StdioTransport
from gateway.proxy.aggregator import DownstreamConnection, ToolAggregator, ToolEntry


# ── Helpers ───────────────────────────────────────────────────


def _make_tool(name: str, description: str = "", schema: dict | None = None) -> Tool:
    """Create a minimal MCP Tool object."""
    return Tool(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


def _make_call_result(text: str = "ok") -> CallToolResult:
    """Create a minimal CallToolResult with a single TextContent block."""
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _mock_session(tools: list[Tool], call_result: CallToolResult | None = None) -> AsyncMock:
    """Build a mock ClientSession that returns the given tools and call result."""
    session = AsyncMock()
    # list_tools returns a result object with .tools and .nextCursor
    list_result = MagicMock()
    list_result.tools = tools
    list_result.nextCursor = None
    session.list_tools.return_value = list_result
    # call_tool
    session.call_tool.return_value = call_result or _make_call_result()
    # initialize
    init_result = MagicMock()
    init_result.serverInfo = MagicMock(name="mock-server")
    init_result.protocolVersion = "2025-06-18"
    session.initialize.return_value = init_result
    return session


@asynccontextmanager
async def _noop_transport(*_args: Any, **_kwargs: Any):
    """Fake transport context manager that yields dummy streams."""
    yield (MagicMock(), MagicMock())


# ── DownstreamConnection tests ────────────────────────────────


@pytest.mark.asyncio
async def test_downstream_connect_stdio():
    """DownstreamConnection.connect opens a stdio transport and initialises the session."""
    config = StdioTransport(command="echo", args=["hello"])
    conn = DownstreamConnection("test-stdio", config)
    mock_session = _mock_session(tools=[_make_tool("greet")])

    stack = AsyncExitStack()
    async with stack:
        with (
            patch("gateway.proxy.aggregator.stdio_client", _noop_transport),
            patch("gateway.proxy.aggregator.ClientSession", return_value=mock_session),
        ):
            await conn.connect(stack)

        mock_session.initialize.assert_awaited_once()
        tools = await conn.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "greet"


@pytest.mark.asyncio
async def test_downstream_connect_sse():
    """DownstreamConnection.connect opens an SSE transport."""
    config = SSETransport(url="https://example.com/mcp/sse")
    conn = DownstreamConnection("test-sse", config)
    mock_session = _mock_session(tools=[_make_tool("search")])

    stack = AsyncExitStack()
    async with stack:
        with (
            patch("gateway.proxy.aggregator.sse_client", _noop_transport),
            patch("gateway.proxy.aggregator.ClientSession", return_value=mock_session),
        ):
            await conn.connect(stack)

        tools = await conn.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "search"


@pytest.mark.asyncio
async def test_downstream_call_tool():
    """call_tool forwards arguments to the session and returns the result."""
    config = StdioTransport(command="echo")
    conn = DownstreamConnection("test", config)
    expected = _make_call_result("42")
    mock_session = _mock_session(tools=[], call_result=expected)

    stack = AsyncExitStack()
    async with stack:
        with (
            patch("gateway.proxy.aggregator.stdio_client", _noop_transport),
            patch("gateway.proxy.aggregator.ClientSession", return_value=mock_session),
        ):
            await conn.connect(stack)

        result = await conn.call_tool("compute", {"x": 7})
        assert result.content[0].text == "42"  # type: ignore[union-attr]
        mock_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_downstream_not_connected_raises():
    """Calling list_tools or call_tool before connect raises RuntimeError."""
    config = StdioTransport(command="echo")
    conn = DownstreamConnection("test", config)

    with pytest.raises(RuntimeError, match="not connected"):
        await conn.list_tools()

    with pytest.raises(RuntimeError, match="not connected"):
        await conn.call_tool("anything", {})


# ── ToolAggregator tests ─────────────────────────────────────


def _patch_transports_and_sessions(sessions_by_name: dict[str, AsyncMock]):
    """Return a context manager that patches both transports + ClientSession.

    The ClientSession constructor is replaced by a factory that returns the
    mock session keyed by the *next* server name in iteration order.
    """
    session_iter = iter(sessions_by_name.values())

    def session_factory(*_args: Any, **_kwargs: Any) -> AsyncMock:
        return next(session_iter)

    return (
        patch("gateway.proxy.aggregator.stdio_client", _noop_transport),
        patch("gateway.proxy.aggregator.sse_client", _noop_transport),
        patch("gateway.proxy.aggregator.ClientSession", side_effect=session_factory),
    )


@pytest.mark.asyncio
async def test_aggregator_merges_manifests():
    """connect_all merges tools from multiple servers under qualified names."""
    servers = {
        "fs": StdioTransport(command="mcp-fs"),
        "gh": StdioTransport(command="mcp-gh"),
    }
    sessions = {
        "fs": _mock_session([_make_tool("read_file"), _make_tool("write_file")]),
        "gh": _mock_session([_make_tool("create_issue"), _make_tool("list_repos")]),
    }

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            manifest = agg.merged_manifest()

    assert len(manifest) == 4
    assert "fs:read_file" in manifest
    assert "fs:write_file" in manifest
    assert "gh:create_issue" in manifest
    assert "gh:list_repos" in manifest

    # Check ToolEntry fields
    entry = manifest["gh:create_issue"]
    assert entry.server_name == "gh"
    assert entry.original_name == "create_issue"


@pytest.mark.asyncio
async def test_aggregator_handles_duplicate_tools():
    """If two servers expose the same qualified name, first-wins and a warning is logged."""
    # This shouldn't normally happen (different server prefixes), but guards
    # against misconfig or if we ever support unprefixed mode.
    servers = {"srv": StdioTransport(command="mcp-a")}
    tool_a = _make_tool("do_thing", description="first")
    sessions = {"srv": _mock_session([tool_a, tool_a])}

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            manifest = agg.merged_manifest()

    # Only one copy kept
    assert len(manifest) == 1
    assert manifest["srv:do_thing"].description == "first"


@pytest.mark.asyncio
async def test_aggregator_call_downstream():
    """call_downstream proxies to the correct server's session."""
    servers = {
        "a": StdioTransport(command="mcp-a"),
        "b": StdioTransport(command="mcp-b"),
    }
    result_a = _make_call_result("from-a")
    result_b = _make_call_result("from-b")
    sessions = {
        "a": _mock_session([_make_tool("ping")], call_result=result_a),
        "b": _mock_session([_make_tool("pong")], call_result=result_b),
    }

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            res_a = await agg.call_downstream("a", "ping", {})
            res_b = await agg.call_downstream("b", "pong", {})

    assert res_a.content[0].text == "from-a"  # type: ignore[union-attr]
    assert res_b.content[0].text == "from-b"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_aggregator_unknown_server_raises():
    """call_downstream with a non-existent server name raises ValueError."""
    servers = {"a": StdioTransport(command="mcp-a")}
    sessions = {"a": _mock_session([])}

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            with pytest.raises(ValueError, match="No downstream server"):
                await agg.call_downstream("nonexistent", "tool", {})


@pytest.mark.asyncio
async def test_aggregator_partial_failure():
    """If one downstream fails to connect, the others still work."""
    servers = {
        "good": StdioTransport(command="mcp-good"),
        "bad": StdioTransport(command="mcp-bad"),
    }

    good_session = _mock_session([_make_tool("hello")])
    bad_session = AsyncMock()
    bad_session.initialize.side_effect = ConnectionError("refused")

    patches_list = list(_patch_transports_and_sessions({"good": good_session, "bad": bad_session}))
    with patches_list[0], patches_list[1], patches_list[2]:
        async with ToolAggregator(servers) as agg:
            manifest = agg.merged_manifest()

    # good server's tool is present; bad server contributed nothing
    assert "good:hello" in manifest
    assert len([k for k in manifest if k.startswith("bad:")]) == 0


@pytest.mark.asyncio
async def test_aggregator_get_tool():
    """get_tool returns a single ToolEntry or None."""
    servers = {"s": StdioTransport(command="mcp-s")}
    sessions = {"s": _mock_session([_make_tool("mytool")])}

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            assert agg.get_tool("s:mytool") is not None
            assert agg.get_tool("s:nonexistent") is None


@pytest.mark.asyncio
async def test_aggregator_tools_for_server():
    """tools_for_server returns only tools from the named server."""
    servers = {
        "fs": StdioTransport(command="mcp-fs"),
        "gh": StdioTransport(command="mcp-gh"),
    }
    sessions = {
        "fs": _mock_session([_make_tool("read"), _make_tool("write")]),
        "gh": _mock_session([_make_tool("issue")]),
    }

    patches = _patch_transports_and_sessions(sessions)
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            fs_tools = agg.tools_for_server("fs")
            gh_tools = agg.tools_for_server("gh")

    assert len(fs_tools) == 2
    assert len(gh_tools) == 1
    assert all(e.server_name == "fs" for e in fs_tools.values())


@pytest.mark.asyncio
async def test_aggregator_refresh_manifest():
    """refresh_manifest re-fetches tools, picking up changes."""
    servers = {"s": StdioTransport(command="mcp-s")}
    mock_session = _mock_session([_make_tool("v1_tool")])

    patches = _patch_transports_and_sessions({"s": mock_session})
    with patches[0], patches[1], patches[2]:
        async with ToolAggregator(servers) as agg:
            assert "s:v1_tool" in agg.merged_manifest()

            # Simulate downstream adding a new tool
            new_list = MagicMock()
            new_list.tools = [_make_tool("v1_tool"), _make_tool("v2_tool")]
            new_list.nextCursor = None
            mock_session.list_tools.return_value = new_list

            await agg.refresh_manifest()
            manifest = agg.merged_manifest()

    assert "s:v1_tool" in manifest
    assert "s:v2_tool" in manifest
