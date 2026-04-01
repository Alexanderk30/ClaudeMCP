"""Usage logging for tool calls.

Tracks tenant, tool name, latency, and success/failure in an in-memory
ring buffer. Meant to feed dashboards or billing down the road.
"""

from __future__ import annotations

import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, NamedTuple

import structlog

logger = structlog.get_logger()


@dataclass
class UsageRecord:
    tenant_id: str
    tool_name: str
    timestamp: float          # wall-clock
    latency_ms: float
    success: bool = True
    error: str | None = None


class UsageStats(NamedTuple):
    total_calls: int
    successes: int
    failures: int
    avg_latency_ms: float
    p99_latency_ms: float


class UsageLogger:
    """Bounded in-memory ring buffer of usage records."""

    def __init__(self, max_records: int = 50_000) -> None:
        self._records: deque[UsageRecord] = deque(maxlen=max_records)

    @asynccontextmanager
    async def track(self, tenant_id: str, tool_name: str) -> AsyncIterator[None]:
        """Context manager that times a tool call and logs the outcome."""
        start = time.monotonic()
        rec = UsageRecord(
            tenant_id=tenant_id, tool_name=tool_name,
            timestamp=time.time(), latency_ms=0.0,
        )
        try:
            yield
            rec.success = True
        except Exception as exc:
            rec.success = False
            rec.error = str(exc)
            raise
        finally:
            rec.latency_ms = (time.monotonic() - start) * 1000
            self._records.append(rec)
            logger.info(
                "gateway.tool_call",
                tenant=tenant_id, tool=tool_name,
                latency_ms=round(rec.latency_ms, 2),
                success=rec.success,
            )

    def query(
        self, *,
        tenant_id: str | None = None,
        tool_name: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """Return recent records matching filters (newest first)."""
        out: list[UsageRecord] = []
        for rec in reversed(self._records):
            if tenant_id and rec.tenant_id != tenant_id:
                continue
            if tool_name and rec.tool_name != tool_name:
                continue
            if since is not None and rec.timestamp < since:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def stats_for(
        self, *,
        tenant_id: str | None = None,
        tool_name: str | None = None,
        since: float | None = None,
    ) -> UsageStats:
        records = self.query(tenant_id=tenant_id, tool_name=tool_name,
                             since=since, limit=50_000)
        if not records:
            return UsageStats(0, 0, 0, 0.0, 0.0)

        latencies = sorted(r.latency_ms for r in records)
        ok = sum(1 for r in records if r.success)
        p99_idx = max(0, int(len(latencies) * 0.99) - 1)
        return UsageStats(
            total_calls=len(records),
            successes=ok,
            failures=len(records) - ok,
            avg_latency_ms=sum(latencies) / len(latencies),
            p99_latency_ms=latencies[p99_idx],
        )

    @property
    def record_count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()
