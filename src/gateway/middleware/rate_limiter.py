"""Layer 2 — Per-tenant sliding-window rate limiter.

Uses an in-memory token-bucket approach (no external dependencies).
Each tenant gets its own bucket based on their configured rate_limit (req/min).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from gateway.config.loader import TenantConfig


class RateLimitExceeded(Exception):
    """Raised when a tenant exceeds their request quota."""


@dataclass
class _Bucket:
    """Simple sliding-window rate limiter for one tenant."""

    max_requests: int
    window_seconds: float = 60.0
    timestamps: list[float] = field(default_factory=list)

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        if len(self.timestamps) >= self.max_requests:
            return False
        self.timestamps.append(now)
        return True


class RateLimiter:
    """Manages per-tenant rate limit buckets."""

    def __init__(self, tenants: dict[str, TenantConfig]) -> None:
        self._buckets: dict[str, _Bucket] = {
            tid: _Bucket(max_requests=t.rate_limit) for tid, t in tenants.items()
        }

    def check(self, tenant_id: str) -> None:
        """Allow the request or raise RateLimitExceeded."""
        bucket = self._buckets.get(tenant_id)
        if bucket is None:
            raise RateLimitExceeded(f"Unknown tenant '{tenant_id}'")
        if not bucket.allow():
            raise RateLimitExceeded(
                f"Tenant '{tenant_id}' exceeded rate limit"
            )
