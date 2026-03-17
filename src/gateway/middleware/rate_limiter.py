"""Layer 2a — Per-tenant sliding-window rate limiter.

Uses an in-memory sliding-window approach (no external dependencies).
Each tenant gets its own bucket based on their configured rate_limit (req/min).

The implementation is safe for single-threaded asyncio and exposes
remaining-capacity introspection so the server layer can return
``X-RateLimit-Remaining``-style metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import NamedTuple

import structlog

from gateway.config.loader import TenantConfig

logger = structlog.get_logger()


class RateLimitExceeded(Exception):
    """Raised when a tenant exceeds their request quota."""

    def __init__(self, tenant_id: str, limit: int, retry_after: float) -> None:
        self.tenant_id = tenant_id
        self.limit = limit
        self.retry_after = retry_after  # seconds until a slot opens
        super().__init__(
            f"Tenant '{tenant_id}' exceeded rate limit "
            f"({limit} req/min). Retry after {retry_after:.1f}s."
        )


class RateStatus(NamedTuple):
    """Snapshot of a tenant's rate-limit state after a check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # 0.0 if allowed


@dataclass
class _Bucket:
    """Sliding-window counter for one tenant."""

    max_requests: int
    window_seconds: float = 60.0
    _timestamps: list[float] = field(default_factory=list)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]

    def allow(self) -> RateStatus:
        """Check and record a request.  Returns a RateStatus."""
        now = time.monotonic()
        self._prune(now)

        if len(self._timestamps) >= self.max_requests:
            retry_after = self._timestamps[0] + self.window_seconds - now
            return RateStatus(
                allowed=False,
                limit=self.max_requests,
                remaining=0,
                retry_after=max(retry_after, 0.0),
            )

        self._timestamps.append(now)
        return RateStatus(
            allowed=True,
            limit=self.max_requests,
            remaining=self.max_requests - len(self._timestamps),
            retry_after=0.0,
        )

    def peek(self) -> RateStatus:
        """Check current state *without* recording a request."""
        now = time.monotonic()
        self._prune(now)
        used = len(self._timestamps)
        if used >= self.max_requests:
            retry_after = self._timestamps[0] + self.window_seconds - now
            return RateStatus(False, self.max_requests, 0, max(retry_after, 0.0))
        return RateStatus(True, self.max_requests, self.max_requests - used, 0.0)


class RateLimiter:
    """Manages per-tenant rate-limit buckets."""

    def __init__(self, tenants: dict[str, TenantConfig]) -> None:
        self._buckets: dict[str, _Bucket] = {
            tid: _Bucket(max_requests=t.rate_limit) for tid, t in tenants.items()
        }

    # ── public API ────────────────────────────────────────

    def check(self, tenant_id: str) -> RateStatus:
        """Record a request and return rate status.  Raises on over-limit."""
        bucket = self._get_bucket(tenant_id)
        status = bucket.allow()
        if not status.allowed:
            raise RateLimitExceeded(
                tenant_id=tenant_id,
                limit=status.limit,
                retry_after=status.retry_after,
            )
        return status

    def peek(self, tenant_id: str) -> RateStatus:
        """Check remaining capacity without consuming a slot."""
        return self._get_bucket(tenant_id).peek()

    def reload(self, tenants: dict[str, TenantConfig]) -> None:
        """Hot-reload tenant configs.

        Existing buckets whose rate_limit hasn't changed keep their state.
        New tenants get fresh buckets.  Removed tenants are dropped.
        """
        new_buckets: dict[str, _Bucket] = {}
        for tid, cfg in tenants.items():
            old = self._buckets.get(tid)
            if old is not None and old.max_requests == cfg.rate_limit:
                new_buckets[tid] = old
            else:
                new_buckets[tid] = _Bucket(max_requests=cfg.rate_limit)
        self._buckets = new_buckets
        logger.info("rate_limiter.reloaded", tenant_count=len(new_buckets))

    # ── internals ─────────────────────────────────────────

    def _get_bucket(self, tenant_id: str) -> _Bucket:
        bucket = self._buckets.get(tenant_id)
        if bucket is None:
            raise RateLimitExceeded(tenant_id=tenant_id, limit=0, retry_after=0)
        return bucket
