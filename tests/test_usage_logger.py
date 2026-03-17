"""Tests for the usage logger (Layer 2b)."""

import asyncio
import time

import pytest

from gateway.middleware.usage_logger import UsageLogger, UsageRecord, UsageStats


@pytest.mark.asyncio
async def test_track_records_success() -> None:
    logger = UsageLogger()
    async with logger.track("tenant-a", "fs:read_file"):
        pass  # simulate work
    assert logger.record_count == 1
    rec = logger.query()[0]
    assert rec.tenant_id == "tenant-a"
    assert rec.tool_name == "fs:read_file"
    assert rec.success is True
    assert rec.latency_ms >= 0


@pytest.mark.asyncio
async def test_track_records_failure() -> None:
    logger = UsageLogger()
    with pytest.raises(ValueError):
        async with logger.track("tenant-b", "gh:create_issue"):
            raise ValueError("boom")
    assert logger.record_count == 1
    rec = logger.query()[0]
    assert rec.success is False
    assert rec.error == "boom"


@pytest.mark.asyncio
async def test_query_by_tenant() -> None:
    logger = UsageLogger()
    async with logger.track("a", "tool1"):
        pass
    async with logger.track("b", "tool2"):
        pass
    async with logger.track("a", "tool3"):
        pass

    results = logger.query(tenant_id="a")
    assert len(results) == 2
    assert all(r.tenant_id == "a" for r in results)


@pytest.mark.asyncio
async def test_query_by_tool() -> None:
    logger = UsageLogger()
    async with logger.track("a", "fs:read"):
        pass
    async with logger.track("a", "fs:write"):
        pass

    results = logger.query(tool_name="fs:read")
    assert len(results) == 1
    assert results[0].tool_name == "fs:read"


@pytest.mark.asyncio
async def test_query_limit() -> None:
    logger = UsageLogger()
    for i in range(10):
        async with logger.track("a", f"tool{i}"):
            pass
    results = logger.query(limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_stats_for() -> None:
    logger = UsageLogger()
    for _ in range(5):
        async with logger.track("a", "fast"):
            pass
    with pytest.raises(RuntimeError):
        async with logger.track("a", "fail"):
            raise RuntimeError("oops")

    stats = logger.stats_for(tenant_id="a")
    assert stats.total_calls == 6
    assert stats.successes == 5
    assert stats.failures == 1
    assert stats.avg_latency_ms >= 0
    assert stats.p99_latency_ms >= 0


@pytest.mark.asyncio
async def test_empty_stats() -> None:
    logger = UsageLogger()
    stats = logger.stats_for()
    assert stats == UsageStats(0, 0, 0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_ring_buffer_eviction() -> None:
    logger = UsageLogger(max_records=5)
    for i in range(10):
        async with logger.track("a", f"tool{i}"):
            pass
    assert logger.record_count == 5
    # Oldest records should be evicted; newest should remain
    names = [r.tool_name for r in logger.query(limit=10)]
    assert "tool9" in names
    assert "tool0" not in names


def test_clear() -> None:
    logger = UsageLogger()
    # Manually append a record to avoid async
    logger._records.append(
        UsageRecord("a", "t", time.time(), 1.0)
    )
    assert logger.record_count == 1
    logger.clear()
    assert logger.record_count == 0
