"""Simple in-memory sliding-window rate limiter for ingest endpoints."""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status


class RateLimiter:
    """Token-bucket style rate limiter keyed by client token."""

    def __init__(self, max_calls: int = 60, window_s: int = 60):
        self.max_calls = max_calls
        self.window_s = window_s
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_s
        self._hits[key] = [t for t in hits if t > cutoff]
        if len(self._hits[key]) >= self.max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit: max {self.max_calls} requests per {self.window_s}s",
            )
        self._hits[key].append(now)


ingest_limiter = RateLimiter(max_calls=120, window_s=60)
