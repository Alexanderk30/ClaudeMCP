"""Proxy layer: connect to downstream MCP servers and merge their tools.

Each downstream runs in its own async context (stdio subprocess or SSE).
Tools get namespaced as "server:tool" to avoid collisions.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, Tool

from gateway.config.loader import DownstreamServerConfig, SSETransport, StdioTransport

logger = structlog.get_logger()


@dataclass
class ToolEntry:
    """A single tool from a downstream, stored under its qualified name."""

    server_name: str
    original_name: str
    tool: Tool

    @property
    def description(self) -> str:
        return self.tool.description or ""

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.tool.inputSchema


class DownstreamConnection:
    """Wraps an MCP ClientSession to one downstream server."""

    def __init__(self, name: str, config: DownstreamServerConfig) -> None:
        self.name = name
        self.config = config
        self._session: ClientSession | None = None

    async def connect(self, stack: AsyncExitStack) -> None:
        log = logger.bind(server=self.name, transport=self.config.transport)
        log.info("downstream.connecting")

        read_stream, write_stream = await self._open_transport(stack)

        self._session = ClientSession(read_stream, write_stream)
        await stack.enter_async_context(self._session)
        init_result = await self._session.initialize()

        log.info(
            "downstream.connected",
            server_info=init_result.serverInfo.name if init_result.serverInfo else "unknown",
            protocol=init_result.protocolVersion,
        )

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if isinstance(self.config, StdioTransport):
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )
            return await stack.enter_async_context(stdio_client(params))

        if isinstance(self.config, SSETransport):
            return await stack.enter_async_context(sse_client(self.config.url))

        raise ValueError(f"Unknown transport type: {type(self.config)}")

    async def list_tools(self) -> list[Tool]:
        """Fetch tools, handling pagination if the server uses cursors."""
        self._assert_connected()
        assert self._session is not None

        all_tools: list[Tool] = []
        cursor: str | None = None

        while True:
            from mcp.types import PaginatedRequestParams
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await self._session.list_tools(params=params)  # type: ignore[arg-type]
            all_tools.extend(result.tools)
            if not result.nextCursor:
                break
            cursor = result.nextCursor

        logger.debug("downstream.tools_listed", server=self.name, count=len(all_tools))
        return all_tools

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any],
        *, timeout_seconds: float = 120,
    ) -> CallToolResult:
        self._assert_connected()
        assert self._session is not None

        logger.debug("downstream.call_tool", server=self.name, tool=tool_name)
        return await self._session.call_tool(
            name=tool_name, arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        )

    def _assert_connected(self) -> None:
        if self._session is None:
            raise RuntimeError(
                f"Downstream '{self.name}' not connected — call connect() first"
            )


@dataclass
class ToolAggregator:
    """Connects to all downstreams and builds a unified tool manifest.

    Use as an async context manager::

        async with ToolAggregator(servers) as agg:
            manifest = agg.merged_manifest()
            result = await agg.call_downstream("github", "create_issue", {...})
    """

    _server_configs: dict[str, DownstreamServerConfig]
    _connections: dict[str, DownstreamConnection] = field(default_factory=dict, init=False)
    _manifest: dict[str, ToolEntry] = field(default_factory=dict, init=False)
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)

    async def __aenter__(self) -> ToolAggregator:
        await self._stack.__aenter__()
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return await self._stack.__aexit__(*exc)

    async def connect_all(self) -> None:
        """Open connections sequentially.

        We can't use asyncio.gather here because the MCP SDK's transports
        use anyio task groups that need to enter/exit from the same task.
        """
        self._connections = {
            name: DownstreamConnection(name, cfg)
            for name, cfg in self._server_configs.items()
        }

        for name, conn in self._connections.items():
            try:
                await conn.connect(self._stack)
            except Exception as exc:
                logger.error("downstream.connect_failed", server=name, error=str(exc))

        await self._build_manifest()

    async def _build_manifest(self) -> None:
        self._manifest.clear()

        for srv_name, conn in self._connections.items():
            if conn._session is None:
                continue  # failed connection, skip

            try:
                tools = await conn.list_tools()
            except Exception as exc:
                logger.error("downstream.list_tools_failed", server=srv_name, error=str(exc))
                continue

            for tool in tools:
                qname = f"{srv_name}:{tool.name}"
                if qname in self._manifest:
                    logger.warning(
                        "aggregator.duplicate_tool", qualified_name=qname,
                        kept_from=self._manifest[qname].server_name,
                    )
                    continue
                self._manifest[qname] = ToolEntry(
                    server_name=srv_name, original_name=tool.name, tool=tool,
                )

        logger.info("aggregator.ready", tool_count=len(self._manifest))

    async def refresh_manifest(self) -> None:
        """Re-fetch tool lists (useful if a downstream's tools changed)."""
        await self._build_manifest()

    def merged_manifest(self) -> dict[str, ToolEntry]:
        return dict(self._manifest)

    def get_tool(self, qualified_name: str) -> ToolEntry | None:
        return self._manifest.get(qualified_name)

    def tools_for_server(self, server_name: str) -> dict[str, ToolEntry]:
        return {
            qn: e for qn, e in self._manifest.items()
            if e.server_name == server_name
        }

    async def call_downstream(
        self, server_name: str, tool_name: str, arguments: dict[str, Any],
        *, timeout_seconds: float = 120,
    ) -> CallToolResult:
        """Forward a tool call to the right downstream server."""
        conn = self._connections.get(server_name)
        if conn is None:
            raise ValueError(f"No downstream server named '{server_name}'")
        return await conn.call_tool(
            tool_name, arguments, timeout_seconds=timeout_seconds,
        )

    async def disconnect_all(self) -> None:
        """Tear down connections explicitly (also happens on context exit)."""
        await self._stack.aclose()
        self._connections.clear()
        self._manifest.clear()
