"""A minimal MCP server that serves as a test downstream.

Run standalone:
    python -m tests.mock_downstream

Or import ``create_mock_server`` to embed in integration tests.
The server exposes two tools — ``echo`` and ``add`` — over stdio transport.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence

from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool


def create_mock_server(name: str = "mock-downstream") -> Server:
    """Build a mock MCP server with deterministic test tools."""
    server = Server(name=name)

    @server.list_tools()  # type: ignore[misc]
    async def handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name="echo",
                description="Echoes back the input message.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Message to echo"},
                    },
                    "required": ["message"],
                },
            ),
            Tool(
                name="add",
                description="Adds two numbers.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                },
            ),
        ]

    @server.call_tool()  # type: ignore[misc]
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> Sequence[TextContent]:
        args = arguments or {}
        if name == "echo":
            return [TextContent(type="text", text=args.get("message", ""))]
        elif name == "add":
            result = args.get("a", 0) + args.get("b", 0)
            return [TextContent(type="text", text=str(result))]
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def main() -> None:
    """Run the mock server over stdio."""
    from mcp.server.stdio import stdio_server

    server = create_mock_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
