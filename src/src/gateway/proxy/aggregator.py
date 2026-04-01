"""Layer 4 — Proxy & Aggregation.

Manages connections to downstream MCP servers and merges their tool
manifests into a single namespace using "server:tool" qualified names.

Each downstream server runs in its own async context (stdio subprocess or
SSE HTTP connection).  The ToolAggregator owns the lifecycle of every
connection and exposes a unified view of all available tools.
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


# ── Data structures ──────────────────────────────────────────


@dataclass
class ToolEntry:
    """One tool from a downstream server, stored under its qualified name."""

    server_name: str
    original_name: str
    tool: Tool  # full MCP Tool object (preserves schema, annotations, etc.)

    @property
    def description(self) -> str:
        return self.tool.description or ""

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.tool.inputSchema


# ── Single downstream connection ─────────────────────────────


class DownstreamConnection:
    """Wraps an MCP ClientSession to a single downstream server.

    The connection is lazily established via :meth:`connect` and torn down
    when the parent :class:`ToolAggregator`'s ``AsyncExitStack`` unwinds.
    """

    def __init__(self, name: str, config: DownstreamServerConfig) -> None:
        self.name = name
        self.config = config
        self._session: ClientSession | None = None

    # ── lifecycle ─────────────────────────────────────────

    async def connect(self, stack: AsyncExitStack) -> None:
        """Open the transport and create + initialise the MCP session.

        The *stack* keeps the async context managers alive for the
        lifetime of the aggregator.
        """
        log = logger.bind(server=self.name, transport=self.config.transport)
        log.info("downstream.connecting")

        read_stream, write_stream = await self._open_transport(stack)

        self._session = ClientSession(read_stream, write_stream)
        # ClientSession.__aenter__ starts the internal message loop
        await stack.enter_async_context(self._session)
        init_result = await self._session.initialize()

        log.info(
            "downstream.connected",
            server_info=init_result.serverInfo.name if init_result.serverInfo else "unknown",
            protocol=init_result.protocolVersion,
        )

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Open the appropriate transport and return (read, write) streams."""
        if isinstance(self.config, StdioTransport):
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )
            return await stack.enter_async_context(stdio_client(params))

        if isinstance(self.config, SSETransport):
            return await stack.enter_async_context(sse_client(self.config.url))

        raise ValueError(f"Unknown transport config type: {type(self.config)}")

    # ── tool operations ───────────────────────────────────

    async def list_tools(self) -> list[Tool]:
        """Fetch the full tool manifest from this downstream server.

        Handles pagination automatically — if the server returns a
        ``nextCursor`` we keep requesting until exhausted.
        """
        self._assert_connected()
        assert self._session is not None

        all_tools: list[Tool] = []
        cursor: str | None = None

        while True:
            from mcp.types import PaginatedRequestParams

            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await self._session.list_tools(params=params)  # type: ignore[arg-type]
            all_tools.extend(result.tools)

            if result.nextCursor:
                cursor = result.nextCursor
            else:
                break

        logger.debug("downstream.tools_listed", server=self.name, count=len(all_tools))
        return all_tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 120,
    ) -> CallToolResult:
        """Forward a tool call to this downstream server."""
        self._assert_connected()
        assert self._session is not None

        logger.debug("downstream.call_tool", server=self.name, tool=tool_name)
        result = await self._session.call_tool(
            name=tool_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        )
        return result

    # ── helpers ────────────────────────────────────────────

    def _assert_connected(self) -> None:
        if self._session is None:
            raise RuntimeError(
                f"Downstream server '{self.name}' is not connected. "
                "Call connect() first."
            )


# ── Aggregator ────────────────────────────────────────────────


@dataclass
class ToolAggregator:
    """Connects to all downstream servers and merges their tool manifests.

    Usage::

        async with ToolAggregator(servers) as aggregator:
            manifest = aggregator.merged_manifest()
            result   = await aggregator.call_downstream("github", "create_issue", {...})

    The async context manager owns every downstream connection.  Exiting
    the context tears them all down gracefully.
    """

    _server_configs: dict[str, DownstreamServerConfig]
    _connections: dict[str, DownstreamConnection] = field(default_factory=dict, init=False)
    _manifest: dict[str, ToolEntry] = field(default_factory=dict, init=False)
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)

    # ── async context manager ─────────────────────────────

    async def __aenter__(self) -> ToolAggregator:
        await self._stack.__aenter__()
        await self.connect_all()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return await self._stack.__aexit__(*exc)

    # ── lifecycle ─────────────────────────────────────────

    async def connect_all(self) -> None:
        """Open connections to every downstream server.

        Connections are opened sequentially because the MCP SDK's transports
        use anyio task groups that must be entered and exited from the same
        asyncio task.  ``asyncio.gather`` would spawn new tasks and break
        that invariant on teardown.
        """
        self._connections = {
            name: DownstreamConnection(name, cfg)
            for name, cfg in self._server_configs.items()
        }

        for name, conn in self._connections.items():
            try:
                await conn.connect(self._stack)
            except Exception as exc:
                logger.error(
                    "downstream.connect_failed",
                    server=name,
                    error=str(exc),
                )

        # Build the merged manifest from every connected server
        await self._build_manifest()

    async def _build_manifest(self) -> None:
        """Fetch tool lists from all connected servers and merge them.

        Sequential iteration (same reason as ``connect_all``): the sessions
        are bound to the current task via anyio.
        """
        self._manifest.clear()

        for server_name, conn in self._connections.items():
            if conn._session is None:
                continue  # skip failed connections

            try:
                tools = await conn.list_tools()
            except Exception as exc:
                logger.error(
                    "downstream.list_tools_failed",
                    server=server_name,
                    error=str(exc),
                )
                continue

            for tool in tools:
                qname = f"{server_name}:{tool.name}"
                if qname in self._manifest:
                    logger.warning(
                        "aggregator.duplicate_tool",
                        qualified_name=qname,
                        kept_from=self._manifest[qname].server_name,
                    )
                    continue
                self._manifest[qname] = ToolEntry(
                    server_name=server_name,
                    original_name=tool.name,
                    tool=tool,
                )

        logger.info("aggregator.ready", tool_count=len(self._manifest))

    async def refresh_manifest(self) -> None:
        """Re-fetch tool lists from all downstream servers.

        Useful if a downstream server's tools change at runtime.
        """
        await self._build_manifest()

    # ── queries ───────────────────────────────────────────

    def merged_manifest(self) -> dict[str, ToolEntry]:
        """Return the unified tool manifest (qualified_name → ToolEntry)."""
        return dict(self._manifest)

    def get_tool(self, qualified_name: str) -> ToolEntry | None:
        """Look up a single tool by qualified name."""
        return self._manifest.get(qualified_name)

    def tools_for_server(self, server_name: str) -> dict[str, ToolEntry]:
        """Return all tools belonging to a specific downstream server."""
        return {
            qname: entry
            for qname, entry in self._manifest.items()
            if entry.server_name == server_name
        }

    # ── tool execution ────────────────────────────────────

    async def call_downstream(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 120,
    ) -> CallToolResult:
        """Proxy a tool call to the named downstream server.

        Parameters
        ----------
        server_name:
            The key used in the config's ``downstream_servers`` map.
        tool_name:
            The *original* (unqualified) tool name on the downstream server.
        arguments:
            The tool's input arguments.
        timeout_seconds:
            Per-call read timeout (default 120 s).
        """
        conn = self._connections.get(server_name)
        if conn is None:
            raise ValueError(f"No downstream server named '{server_name}'")

        return await conn.call_tool(
            tool_name,
            arguments,
            timeout_seconds=timeout_seconds,
        )

    async def disconnect_all(self) -> None:
        """Explicitly tear down all connections.

        This is called automatically when used as an async context manager,
        but can be invoked directly if needed.
        """
        await self._stack.aclose()
        self._connections.clear()
        self._manifest.clear()
