"""Live test against the running server, through a real MCP client.

Modest and polite: one read per site, a pause between calls, about 10
searches. Writes a summary (no page contents, only lengths, methods and
timings) to live-results/, which is not committed.

    uv run python scripts/live_test.py [--url http://127.0.0.1:8890/mcp]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from mcp import Client

# The home IP was already rate-limited by other agents' tests, so this stays
# small: 6 searches spaced 10 s apart, then one repeat that the cache answers.
SEARCHES = [
    "python asyncio timeout context manager",
    "Camoufox anti-detect browser Playwright",
    "latest news Linux kernel release",
    "how does HTTP/2 multiplexing work",
    "Model Context Protocol streamable HTTP transport",
    "Wayback Machine availability API",
]

ORDINARY = [
    "https://docs.python.org/3/library/asyncio-task.html",
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://github.com/daijro/camoufox",
    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/429",
    "https://www.bbc.com/news",
    "https://www.theguardian.com/international",
    "https://arxiv.org/abs/1706.03762",
    "https://arxiv.org/pdf/1706.03762",
    "https://api.github.com/repos/searxng/searxng",
    "https://www.rfc-editor.org/rfc/rfc9110.txt",
    "https://news.ycombinator.com/",
    "https://yle.fi/news",
    "https://docs.docker.com/compose/",
    "https://peps.python.org/pep-0008/",
    "https://apnews.com/",
]

PROTECTED = [
    "https://www.reddit.com/r/LocalLLaMA/comments/1uyjdfg/added_searxng_and_i_dont_even_know_what_to_say/",
    "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python",
    "https://www.reuters.com/technology/",
    "https://www.zillow.com/homes/for_sale/",
    "https://www.ticketmaster.com/",
    "https://www.etsy.com/",
    "https://www.walmart.com/",
    "https://www.g2.com/",
    "https://www.glassdoor.com/index.htm",
    "https://nowsecure.nl/",
]


def health(base: str) -> dict:
    with urllib.request.urlopen(base.replace("/mcp", "/health"), timeout=5) as r:
        return json.loads(r.read())


def parse_read(text: str) -> dict:
    m = re.search(r"^Fetched via: (\w+)", text, re.M)
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else ""
    total = re.search(r"showing [\d,]+ of ([\d,]+) characters", text)
    return {
        "method": m.group(1) if m else None,
        "chars": int(total.group(1).replace(",", "")) if total else len(body),
        "has_title": not re.search(r"^Title: \(none\)", text, re.M),
    }


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8890/mcp")
    ap.add_argument("--pause", type=float, default=3.0)
    ap.add_argument("--skip-search", action="store_true")
    args = ap.parse_args()

    before = health(args.url)
    out: dict = {"searches": [], "reads": [], "repeat": []}
    async with Client(args.url) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        out["tools"] = tools
        print("tools:", tools)

        if not args.skip_search:
            for q in SEARCHES + [SEARCHES[0].upper()]:  # the last one should come from the cache
                t0 = time.monotonic()
                r = await c.call_tool("web_search", {"query": q, "max_results": 6})
                ms = int((time.monotonic() - t0) * 1000)
                text = r.content[0].text if r.content else ""
                prov = re.search(r"provider: ([\w:\- ]+);", text)
                n = len(re.findall(r"^\d+\. ", text, re.M))
                out["searches"].append({"ok": not r.is_error and n > 0, "provider": prov.group(1) if prov else None, "results": n, "ms": ms})
                print(f"search  {'ok ' if n else 'ERR'} {ms:6d} ms  {n} results  {prov.group(1) if prov else text[:80]}")
                await asyncio.sleep(10.0)

        for group, urls in (("ordinary", ORDINARY), ("protected", PROTECTED)):
            for u in urls:
                t0 = time.monotonic()
                r = await c.call_tool("read_page", {"url": u, "max_chars": 4000})
                ms = int((time.monotonic() - t0) * 1000)
                text = r.content[0].text if r.content else ""
                row = {"group": group, "domain": urlsplit(u).hostname, "ok": not r.is_error, "ms": ms}
                if r.is_error:
                    row["error"] = text.replace("Error executing tool read_page: ", "")[:300]
                else:
                    row.update(parse_read(text))
                out["reads"].append(row)
                print(f"{group:9s} {'ok ' if row['ok'] else 'ERR'} {ms:6d} ms  {row.get('method') or '-':8s} {row['domain']}  {row.get('chars', '')} {row.get('error', '')[:140]}")
                await asyncio.sleep(args.pause)

        # Domain memory and cache: one more read of a protected site that went
        # to the browser (should skip http), and a cached repeat.
        browser_rows = [r for r in out["reads"] if r.get("method") == "browser" and r["group"] == "protected"]
        repeats = []
        if browser_rows:
            dom = browser_rows[0]["domain"]
            repeats.append(next(u for u in PROTECTED if urlsplit(u).hostname == dom))
        repeats.append(ORDINARY[1])
        for u in repeats:
            t0 = time.monotonic()
            r = await c.call_tool("read_page", {"url": u, "max_chars": 2000})
            ms = int((time.monotonic() - t0) * 1000)
            text = r.content[0].text if r.content else ""
            via = re.search(r"^Fetched via: (.+)$", text, re.M)
            out["repeat"].append({"domain": urlsplit(u).hostname, "ok": not r.is_error, "via": via.group(1) if via else None, "ms": ms})
            print(f"repeat   {ms:6d} ms  {via.group(1) if via else text[:100]}  {urlsplit(u).hostname}")

    after = health(args.url)
    stats = {k: after["read_stats"].get(k, 0) - before["read_stats"].get(k, 0) for k in set(after["read_stats"]) | set(before["read_stats"])}
    out["step_stats"] = stats
    reads = out["reads"]
    ok_ms = [r["ms"] for r in reads if r["ok"]]
    summary = {
        "searches_ok": f"{sum(s['ok'] for s in out['searches'])}/{len(out['searches'])}",
        "search_ms_median": statistics.median([s["ms"] for s in out["searches"]]) if out["searches"] else None,
        "search_ms_p90": pct([s["ms"] for s in out["searches"]], 90),
        "search_layers": {p: sum(1 for s in out["searches"] if s["provider"] == p) for p in {s["provider"] for s in out["searches"]}},
        "reads_ok": f"{sum(r['ok'] for r in reads)}/{len(reads)}",
        "ordinary_ok": f"{sum(r['ok'] for r in reads if r['group']=='ordinary')}/{sum(1 for r in reads if r['group']=='ordinary')}",
        "protected_ok": f"{sum(r['ok'] for r in reads if r['group']=='protected')}/{sum(1 for r in reads if r['group']=='protected')}",
        "by_method": {m: sum(1 for r in reads if r.get("method") == m) for m in ("http", "browser", "archive")},
        "steps": {
            "http": f"{stats.get('http_ok', 0)}/{stats.get('http_try', 0)}",
            "browser": f"{stats.get('browser_ok', 0)}/{stats.get('browser_try', 0)}",
            "archive": f"{stats.get('archive_ok', 0)}/{stats.get('archive_try', 0)}",
        },
        "read_ms_median_all": statistics.median([r["ms"] for r in reads]),
        "read_ms_p90_all": pct([r["ms"] for r in reads], 90),
        "read_ms_median_ok": statistics.median(ok_ms) if ok_ms else None,
        "read_ms_p90_ok": pct(ok_ms, 90),
        "by_method_ms": {
            m: {"median": statistics.median(v), "p90": pct(v, 90)}
            for m in ("http", "browser", "archive")
            if (v := [r["ms"] for r in reads if r.get("method") == m])
        },
    }
    out["summary"] = summary
    Path("live-results").mkdir(exist_ok=True)
    path = Path("live-results") / time.strftime("live-%Y%m%d-%H%M%S.json")
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps(summary, indent=1))
    print("saved", path)


if __name__ == "__main__":
    asyncio.run(main())
