"""Settings, all from environment variables (see README for the list)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # MCP transport
    host: str = "127.0.0.1"
    port: int = 8890

    # Search
    searxng_url: str = "http://127.0.0.1:8888"
    search_timeout: float = 12.0
    search_rate: str = "3/10"           # outgoing searches: 3 per 10 s, queued
    search_queue_wait: float = 15.0
    search_cache_ttl: float = 3600.0
    search_empty_ttl: float = 300.0
    search_deadline: float = 35.0
    browser_search_engines: str = "startpage,brave"   # empty string turns the layer off
    browser_search_per_min: int = 4
    brave_api_key: str = ""             # optional, off without a key
    exa_api_key: str = ""               # optional, off without a key

    # read_page
    read_deadline: float = 25.0          # overall budget for one read_page call
    http_timeout: float = 12.0           # cap for the plain HTTP step
    challenge_wait: float = 15.0         # how long the browser waits for a challenge to clear
    max_download_bytes: int = 10 * 1024 * 1024
    max_redirects: int = 8
    cache_ttl: float = 3600.0
    cache_size: int = 256
    domain_memory_ttl: float = 24 * 3600.0
    archive_enabled: bool = True

    # Stealth browser
    stealth_enabled: bool = True
    stealth_headless: str = "true"       # "true" or "virtual" (Xvfb inside the container)
    stealth_idle_close: float = 300.0
    stealth_max_pages: int = 2

    # Privacy
    log_details: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("WEB_MCP_HOST", "127.0.0.1"),
            port=_env_int("WEB_MCP_PORT", 8890),
            searxng_url=os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/"),
            search_timeout=_env_float("SEARCH_TIMEOUT", 12.0),
            search_rate=os.environ.get("SEARCH_RATE", "3/10"),
            search_queue_wait=_env_float("SEARCH_QUEUE_WAIT", 15.0),
            search_cache_ttl=_env_float("SEARCH_CACHE_TTL", 3600.0),
            search_empty_ttl=_env_float("SEARCH_EMPTY_TTL", 300.0),
            search_deadline=_env_float("SEARCH_DEADLINE", 35.0),
            browser_search_engines=os.environ.get("BROWSER_SEARCH_ENGINES", "startpage,brave"),
            browser_search_per_min=_env_int("BROWSER_SEARCH_PER_MIN", 4),
            brave_api_key=os.environ.get("BRAVE_API_KEY", "").strip(),
            exa_api_key=os.environ.get("EXA_API_KEY", "").strip(),
            read_deadline=_env_float("READ_DEADLINE", 25.0),
            http_timeout=_env_float("HTTP_TIMEOUT", 12.0),
            challenge_wait=_env_float("CHALLENGE_WAIT", 15.0),
            max_download_bytes=_env_int("MAX_DOWNLOAD_MB", 10) * 1024 * 1024,
            max_redirects=_env_int("MAX_REDIRECTS", 8),
            cache_ttl=_env_float("CACHE_TTL", 3600.0),
            cache_size=_env_int("CACHE_SIZE", 256),
            domain_memory_ttl=_env_float("DOMAIN_MEMORY_TTL", 24 * 3600.0),
            archive_enabled=_env_bool("ARCHIVE_ENABLED", True),
            stealth_enabled=_env_bool("STEALTH_ENABLED", True),
            stealth_headless=os.environ.get("STEALTH_HEADLESS", "true").strip().lower(),
            stealth_idle_close=_env_float("STEALTH_IDLE_CLOSE", 300.0),
            stealth_max_pages=max(1, _env_int("STEALTH_MAX_PAGES", 2)),
            log_details=_env_bool("LOG_DETAILS", False),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
