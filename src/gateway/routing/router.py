"""Tool router — resolves qualified names to downstream servers."""

from __future__ import annotations

import fnmatch
from typing import Any

from mcp.types import CallToolResult

from gateway.proxy.aggregator import ToolAggregator, ToolEntry


class RoutingError(Exception):
    """Tool can't be routed to any downstream."""


class ToolRouter:
    def __init__(self, manifest: dict[str, ToolEntry], aggregator: ToolAggregator) -> None:
        self._manifest = manifest
        self._aggregator = aggregator

    def reload_manifest(self) -> None:
        self._manifest = self._aggregator.merged_manifest()

    def list_tools(self, *, allowed_patterns: list[str] | None = None) -> list[dict[str, Any]]:
        """Merged tool list, optionally filtered by glob patterns."""
        tools: list[dict[str, Any]] = []
        for qname, entry in self._manifest.items():
            if allowed_patterns and not any(
                fnmatch.fnmatch(qname, p) for p in allowed_patterns
            ):
                continue
            tools.append({
                "name": qname,
                "description": entry.description,
                "inputSchema": entry.input_schema,
            })
        return tools

    async def call_tool(
        self, qualified_name: str, arguments: dict[str, Any],
        *, timeout_seconds: float = 120,
    ) -> CallToolResult:
        entry = self._manifest.get(qualified_name)
        if entry is None:
            raise RoutingError(f"Unknown tool: {qualified_name}")
        return await self._aggregator.call_downstream(
            server_name=entry.server_name,
            tool_name=entry.original_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )
