"""Layer 2b — Usage & token logging.

Logs every tool call with tenant, tool name, latency, and token counts
so you can build dashboards or billing later.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog

logger = structlog.get_logger()


@dataclass
class UsageRecord:
    tenant_id: str
    tool_name: str
    timestamp: float
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    success: bool = True
    error: str | None = None


class UsageLogger:
    """Collects usage records in memory (swap for a real store later)."""

    records: list[UsageRecord] = field(default_factory=list)

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    @asynccontextmanager
    async def track(self, tenant_id: str, tool_name: str) -> AsyncIterator[None]:
        """Context manager that logs a usage record when the call finishes."""
        start = time.monotonic()
        record = UsageRecord(
            tenant_id=tenant_id,
            tool_name=tool_name,
            timestamp=time.time(),
            latency_ms=0,
        )
        try:
            yield
            record.success = True
        except Exception as exc:
            record.success = False
            record.error = str(exc)
            raise
        finally:
            record.latency_ms = (time.monotonic() - start) * 1000
            self.records.append(record)
            logger.info(
                "gateway.tool_call",
                tenant=tenant_id,
                tool=tool_name,
                latency_ms=round(record.latency_ms, 2),
                success=record.success,
            )
