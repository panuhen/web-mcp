"""Run extraction in worker processes with time and memory limits.

trafilatura, lxml and pypdf run in C or plain Python and cannot be
interrupted from a thread. A hostile page (deep nesting, millions of tiny
elements, a PDF with a huge object graph) could otherwise burn CPU and memory
long after the read_page deadline. Here every extraction runs in a small pool
of worker processes:

- each worker has an address-space limit (RLIMIT_AS) and a CPU-time limit;
- each job has a wall-clock limit; when it is hit, the pool's processes are
  killed and a fresh pool is started, and the caller gets `TooComplex`.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

log = logging.getLogger("web_mcp.worker")

WORKER_MEMORY_BYTES = 1024 * 1024 * 1024   # 1 GiB address space per worker
WORKER_CPU_SECONDS = 600                    # lifetime CPU cap; the pool restarts if hit


class TooComplex(Exception):
    """An extraction hit the time or memory limit."""


def _init_worker(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ImportError, ValueError, OSError):
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Import the heavy libraries once per worker, not once per job.
    try:
        import lxml.html  # noqa: F401
        import trafilatura  # noqa: F401
    except Exception:
        pass


class ExtractorPool:
    def __init__(self, workers: int = 2, timeout: float = 10.0, memory_bytes: int = WORKER_MEMORY_BYTES):
        self.workers = workers
        self.timeout = timeout
        self.memory_bytes = memory_bytes
        self._pool: ProcessPoolExecutor | None = None
        self.restarts = 0
        self.jobs = 0

    def _ensure(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
                initargs=(self.memory_bytes, WORKER_CPU_SECONDS),
            )
        return self._pool

    def _kill(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        self.restarts += 1
        for p in list(getattr(pool, "_processes", {}).values()):
            try:
                os.kill(p.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        pool.shutdown(wait=False, cancel_futures=True)

    async def run(self, fn, *args, timeout: float | None = None):
        """Run fn(*args) in a worker. Raises TooComplex on the time or memory limit."""
        limit = self.timeout if timeout is None else max(0.5, min(self.timeout, timeout))
        end = time.monotonic() + limit
        for attempt in range(2):
            pool = self._ensure()
            loop = asyncio.get_running_loop()
            try:
                fut = loop.run_in_executor(pool, fn, *args)
            except (BrokenProcessPool, RuntimeError):
                self._kill()
                continue
            self.jobs += 1
            try:
                return await asyncio.wait_for(fut, max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                log.info("extraction hit the time limit; restarting workers")
                if self._pool is pool:
                    self._kill()
                raise TooComplex(f"took longer than {limit:.0f} s") from None
            except asyncio.CancelledError:
                # The caller's deadline: do not leave the job running.
                if self._pool is pool:
                    self._kill()
                raise
            except MemoryError:
                raise TooComplex("needed too much memory") from None
            except BrokenProcessPool:
                # A worker died: our job hit the memory/CPU limit, or another
                # caller's timeout killed the pool. Retry once on a fresh pool.
                if self._pool is pool:
                    self._kill()
                if attempt == 0 and end - time.monotonic() > 0.5:
                    continue
                raise TooComplex("the extractor process died (memory limit)") from None
        raise TooComplex("the extractor is unavailable")

    def close(self) -> None:
        self._kill()
