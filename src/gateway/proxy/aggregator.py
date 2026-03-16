"""Layer 4 — Proxy & Aggregation.

Manages connections to downstream MCP servers and merges their tool
manifests into a single namespace using "server:tool" qualified names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from gateway.config.loader import DownstreamServerConfig

logger = structlog.get_logger()


@dataclass
class ToolEntry:
    """One tool from a downstream server, stored under its qualified name."""

    server_name: str
    original_name: str
    description: str
    input_schema: dict[str, Any]


class DownstreamConnection:
    """Wraps an MCP client session to a single downstream server."""

    def __init__(self, name: str, config: DownstreamServerConfig) -> None:
        self.name = name
        self.config = config
        self._session: Any = None  # Will hold the mcp ClientSession

    async def connect(self) -> None:
        """Establish the MCP client connection (stdio or SSE)."""
        # TODO: use mcp.ClientSession with the appropriate transport
        logger.info("downstream.connecting", server=self.name, transport=self.config.transport)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch the tool manifest from this downstream server."""
        # TODO: call self._session.list_tools()
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Forward a tool call to this downstream server."""
        # TODO: call self._session.call_tool(tool_name, arguments)
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Tear down the connection."""
        # TODO: graceful shutdown
        pass


class ToolAggregator:
    """Connects to all downstream servers and merges their tool manifests."""

    def __init__(self, servers: dict[str, DownstreamServerConfig]) -> None:
        self._connections: dict[str, DownstreamConnection] = {
            name: DownstreamConnection(name, cfg) for name, cfg in servers.items()
        }
        self._manifest: dict[str, ToolEntry] = {}

    async def connect_all(self) -> None:
        """Open connections to every downstream server."""
        for conn in self._connections.values():
            await conn.connect()
            tools = await conn.list_tools()
            for tool in tools:
                qname = f"{conn.name}:{tool['name']}"
                self._manifest[qname] = ToolEntry(
                    server_name=conn.name,
                    original_name=tool["name"],
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                )
        logger.info("aggregator.ready", tool_count=len(self._manifest))

    def merged_manifest(self) -> dict[str, ToolEntry]:
        """Return the unified tool manifest (qualified_name → ToolEntry)."""
        return dict(self._manifest)

    async def call_downstream(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """Proxy a tool call to the named downstream server."""
        conn = self._connections.get(server_name)
        if conn is None:
            raise ValueError(f"No downstream server named '{server_name}'")
        return await conn.call_tool(tool_name, arguments)

    async def disconnect_all(self) -> None:
        for conn in self._connections.values():
            await conn.disconnect()
