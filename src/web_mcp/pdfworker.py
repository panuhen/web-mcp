"""Extract PDF text in a child process.

pypdf runs in plain Python and a crafted PDF can make it spin or allocate a
lot. Running it in a child process with memory and CPU limits means the
read_page deadline can kill it, which a thread could not.

Protocol: PDF bytes on stdin, one JSON object on stdout.
"""

from __future__ import annotations

import asyncio
import json
import sys

MAX_PDF_PAGES = 300
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GiB address space
WORKER_CPU_SECONDS = 60


def _limit_resources() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))
    except (ImportError, ValueError, OSError):
        pass


def main() -> None:
    _limit_resources()
    from .extract import UnsupportedContent, extract_pdf

    body = sys.stdin.buffer.read()
    try:
        ex = extract_pdf(body)
        out = {"ok": True, "text": ex.text, "title": ex.title}
    except UnsupportedContent as e:
        out = {"ok": False, "error": str(e)}
    except MemoryError:
        out = {"ok": False, "error": "PDF needs too much memory"}
    sys.stdout.write(json.dumps(out))


async def extract_pdf_isolated(body: bytes, timeout: float):
    """Run extract_pdf in a child process. Kills it on timeout or cancellation."""
    from .extract import Extracted, UnsupportedContent

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "web_mcp.pdfworker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(body), max(0.5, timeout))
    except asyncio.TimeoutError:
        raise UnsupportedContent("PDF text extraction took too long") from None
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    try:
        data = json.loads(out.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        data = {}
    if not data.get("ok"):
        raise UnsupportedContent(data.get("error") or "could not read PDF")
    return Extracted(text=data["text"], title=data.get("title", ""), kind="pdf")


if __name__ == "__main__":
    main()
