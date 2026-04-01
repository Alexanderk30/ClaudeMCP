"""Structured error types and MCP error formatting.

All gateway-specific exceptions inherit from :class:`GatewayError` so
callers can catch them with a single ``except`` clause.  The
:func:`format_error_result` helper builds an MCP-compatible
``CallToolResult`` with ``isError=True`` for returning errors to clients.
"""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent


class GatewayError(Exception):
    """Base class for all gateway errors."""

    status_code: int = 500

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class ToolCallError(GatewayError):
    """Error during a proxied tool call to a downstream server."""

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        super().__init__(message, status_code=502)
        self.server_name = server_name
        self.tool_name = tool_name


def format_error_result(error: Exception) -> CallToolResult:
    """Build an MCP ``CallToolResult`` that signals an error to the client.

    This is the canonical way to return errors from gateway-level tool
    handlers without raising into the MCP SDK internals.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=str(error))],
        isError=True,
    )
