"""Tests for the rate limiter."""

import pytest

from gateway.config.loader import TenantConfig
from gateway.middleware.rate_limiter import RateLimitExceeded, RateLimiter

_TENANTS = {
    "limited": TenantConfig(
        api_key="sk-ltd",
        role="viewer",
        allowed_tools=["*"],
        rate_limit=3,
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
    with pytest.raises(RateLimitExceeded):
        rl.check("limited")
