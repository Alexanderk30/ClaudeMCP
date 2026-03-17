"""Tests for the rate limiter (Layer 2a)."""

import pytest

from gateway.config.loader import TenantConfig
from gateway.middleware.rate_limiter import RateLimitExceeded, RateLimiter, RateStatus

_TENANTS = {
    "limited": TenantConfig(
        api_key="sk-ltd",
        role="viewer",
        allowed_tools=["*"],
        rate_limit=3,
        downstream=[],
    ),
    "generous": TenantConfig(
        api_key="sk-gen",
        role="admin",
        allowed_tools=["*"],
        rate_limit=100,
        downstream=[],
    ),
}


def test_allows_within_limit() -> None:
    rl = RateLimiter(_TENANTS)
    for _ in range(3):
        rl.check("limited")  # should not raise


def test_rejects_over_limit() -> None:
    rl = RateLimiter(_TENANTS)
    for _ in range(3):
        rl.check("limited")
    with pytest.raises(RateLimitExceeded) as exc_info:
        rl.check("limited")
    assert exc_info.value.tenant_id == "limited"
    assert exc_info.value.limit == 3
    assert exc_info.value.retry_after >= 0


def test_check_returns_rate_status() -> None:
    rl = RateLimiter(_TENANTS)
    status = rl.check("limited")
    assert isinstance(status, RateStatus)
    assert status.allowed is True
    assert status.limit == 3
    assert status.remaining == 2
    assert status.retry_after == 0.0


def test_remaining_decrements() -> None:
    rl = RateLimiter(_TENANTS)
    s1 = rl.check("limited")
    s2 = rl.check("limited")
    s3 = rl.check("limited")
    assert s1.remaining == 2
    assert s2.remaining == 1
    assert s3.remaining == 0


def test_peek_does_not_consume() -> None:
    rl = RateLimiter(_TENANTS)
    status = rl.peek("limited")
    assert status.remaining == 3
    # Peek again — still 3
    assert rl.peek("limited").remaining == 3


def test_unknown_tenant_raises() -> None:
    rl = RateLimiter(_TENANTS)
    with pytest.raises(RateLimitExceeded):
        rl.check("nonexistent")


def test_reload_preserves_existing_buckets() -> None:
    rl = RateLimiter(_TENANTS)
    rl.check("limited")
    rl.check("limited")
    assert rl.peek("limited").remaining == 1

    # Reload with same config — bucket state preserved
    rl.reload(_TENANTS)
    assert rl.peek("limited").remaining == 1


def test_reload_resets_changed_limits() -> None:
    rl = RateLimiter(_TENANTS)
    rl.check("limited")

    new_tenants = {
        "limited": TenantConfig(
            api_key="sk-ltd", role="viewer", allowed_tools=["*"],
            rate_limit=10, downstream=[],  # changed from 3 to 10
        ),
    }
    rl.reload(new_tenants)
    # New bucket — full capacity
    assert rl.peek("limited").remaining == 10
