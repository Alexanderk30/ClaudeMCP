"""Layer 3 — Tool Router.

Maps a qualified tool name (e.g. "github:create_issue") to the correct
downstream MCP server and dispatches the call via the aggregator.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from mcp.types import CallToolResult

from gateway.proxy.aggregator import ToolAggregator, ToolEntry


class RoutingError(Exception):
    """Raised when a tool cannot be routed to any downstream server."""


class ToolRouter:
    """Resolves tool names to downstream servers and dispatches calls."""

    def __init__(self, manifest: dict[str, ToolEntry], aggregator: ToolAggregator) -> None:
        self._manifest = manifest  # qualified_name → ToolEntry
        self._aggregator = aggregator

    def reload_manifest(self) -> None:
        """Refresh from the aggregator's current manifest (e.g. after refresh)."""
        self._manifest = self._aggregator.merged_manifest()

    def list_tools(self, *, allowed_patterns: list[str] | None = None) -> list[dict[str, Any]]:
        """Return the merged tool list, optionally filtered by glob patterns.

        Each returned dict is an MCP-compatible tool descriptor with
        ``name``, ``description``, and ``inputSchema`` keys.
        """
        tools: list[dict[str, Any]] = []
        for qname, entry in self._manifest.items():
            if allowed_patterns:
                if not any(fnmatch.fnmatch(qname, p) for p in allowed_patterns):
                    continue
            tools.append(
                {
                    "name": qname,
                    "description": entry.description,
                    "inputSchema": entry.input_schema,
                }
            )
        return tools

    async def call_tool(
        self,
        qualified_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 120,
    ) -> CallToolResult:
        """Route a tool call to the correct downstream MCP server."""
        entry = self._manifest.get(qualified_name)
        if entry is None:
            raise RoutingError(f"Unknown tool: {qualified_name}")
        return await self._aggregator.call_downstream(
            server_name=entry.server_name,
            tool_name=entry.original_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )
