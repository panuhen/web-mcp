"""In-memory URL cache and per-domain memory. Nothing is written to disk."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def domain_key(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 256, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.max_items = max_items
        self.clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if self.clock() >= expires:
            del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._data[key] = (self.clock() + (self.ttl if ttl is None else ttl), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class DomainMemory:
    """Remembers domains whose plain HTTP step was blocked, so the next reads go straight to the browser."""

    def __init__(self, ttl: float = 24 * 3600.0, clock: Callable[[], float] = time.monotonic, max_items: int = 5000):
        self.ttl = ttl
        self.clock = clock
        self.max_items = max_items
        self._until: dict[str, float] = {}

    def mark_blocked(self, host: str) -> None:
        if len(self._until) >= self.max_items:
            now = self.clock()
            self._until = {k: v for k, v in self._until.items() if v > now}
            if len(self._until) >= self.max_items:
                self._until.pop(next(iter(self._until)))
        self._until[domain_key(host)] = self.clock() + self.ttl

    def is_blocked(self, host: str) -> bool:
        key = domain_key(host)
        until = self._until.get(key)
        if until is None:
            return False
        if self.clock() >= until:
            del self._until[key]
            return False
        return True

    def __len__(self) -> int:
        now = self.clock()
        return sum(1 for v in self._until.values() if v > now)
