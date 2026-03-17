"""Layer 2b — Usage & token logging.

Logs every tool call with tenant, tool name, latency, and success/failure
so you can build dashboards or billing later.

Records are held in-memory with a configurable max-size ring buffer.
A ``query()`` helper makes it easy to pull recent records by tenant/tool,
and ``stats_for()`` returns aggregate counts.
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
    """Single tool-call record."""

    tenant_id: str
    tool_name: str
    timestamp: float  # wall-clock (time.time)
    latency_ms: float
    success: bool = True
    error: str | None = None


class UsageStats(NamedTuple):
    """Aggregate stats for a query window."""

    total_calls: int
    successes: int
    failures: int
    avg_latency_ms: float
    p99_latency_ms: float


class UsageLogger:
    """Collects usage records in a bounded in-memory ring buffer."""

    def __init__(self, max_records: int = 50_000) -> None:
        self._records: deque[UsageRecord] = deque(maxlen=max_records)

    # ── recording ─────────────────────────────────────────

    @asynccontextmanager
    async def track(self, tenant_id: str, tool_name: str) -> AsyncIterator[None]:
        """Wrap a tool call; records timing and success/failure on exit."""
        start = time.monotonic()
        record = UsageRecord(
            tenant_id=tenant_id,
            tool_name=tool_name,
            timestamp=time.time(),
            latency_ms=0.0,
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
            self._records.append(record)
            logger.info(
                "gateway.tool_call",
                tenant=tenant_id,
                tool=tool_name,
                latency_ms=round(record.latency_ms, 2),
                success=record.success,
            )

    # ── querying ──────────────────────────────────────────

    def query(
        self,
        *,
        tenant_id: str | None = None,
        tool_name: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """Return recent records matching the given filters (newest first)."""
        matches: list[UsageRecord] = []
        for rec in reversed(self._records):
            if tenant_id is not None and rec.tenant_id != tenant_id:
                continue
            if tool_name is not None and rec.tool_name != tool_name:
                continue
            if since is not None and rec.timestamp < since:
                continue
            matches.append(rec)
            if len(matches) >= limit:
                break
        return matches

    def stats_for(
        self,
        *,
        tenant_id: str | None = None,
        tool_name: str | None = None,
        since: float | None = None,
    ) -> UsageStats:
        """Compute aggregate statistics over matching records."""
        records = self.query(
            tenant_id=tenant_id, tool_name=tool_name, since=since, limit=50_000
        )
        if not records:
            return UsageStats(0, 0, 0, 0.0, 0.0)

        latencies = sorted(r.latency_ms for r in records)
        successes = sum(1 for r in records if r.success)
        p99_idx = max(0, int(len(latencies) * 0.99) - 1)

        return UsageStats(
            total_calls=len(records),
            successes=successes,
            failures=len(records) - successes,
            avg_latency_ms=sum(latencies) / len(latencies),
            p99_latency_ms=latencies[p99_idx],
        )

    @property
    def record_count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()
