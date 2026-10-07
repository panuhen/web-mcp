"""Token bucket used to pace outgoing searches so a burst cannot get the IP rate-limited."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class RateLimited(Exception):
    """Waiting for a slot would take longer than allowed."""

    def __init__(self, wait: float):
        super().__init__(f"wait {wait:.0f} s")
        self.wait = wait


class TokenBucket:
    """`capacity` requests at once, refilled at `capacity` per `period` seconds."""

    def __init__(self, capacity: int, period: float, clock: Callable[[], float] = time.monotonic):
        self.capacity = max(1, capacity)
        self.rate = self.capacity / max(0.001, period)  # tokens per second
        self.clock = clock
        self._tokens = float(self.capacity)
        self._stamp = clock()
        self._lock = asyncio.Lock()
        self.waited = 0  # how many acquisitions had to queue

    def _refill(self) -> None:
        now = self.clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def try_take(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self, max_wait: float) -> float:
        """Take a token, queueing up to `max_wait` seconds. Returns the time waited."""
        async with self._lock:  # FIFO: callers queue behind each other
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            wait = (1 - self._tokens) / self.rate
            if wait > max_wait:
                raise RateLimited(wait)
            self.waited += 1
            await asyncio.sleep(wait)
            self._refill()
            self._tokens = max(0.0, self._tokens - 1)
            return wait


def parse_rate(spec: str, default: tuple[int, float] = (3, 10.0)) -> tuple[int, float]:
    """'3/10' -> (3, 10.0): three requests per ten seconds."""
    try:
        n, p = spec.split("/")
        return max(1, int(n)), max(0.1, float(p))
    except (ValueError, AttributeError):
        return default
