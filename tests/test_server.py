"""The MCP surface, through a real MCP client connected in-process."""

import httpx
from mcp import Client

from web_mcp.config import Settings
from web_mcp.search import Searcher
from web_mcp.server import create_server


def server():
    mcp = create_server(Settings(stealth_enabled=False, archive_enabled=False))
    payload = {"results": [{"title": "T", "url": "https://t.example/", "content": "c", "engines": ["bing"]}], "unresponsive_engines": []}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    mcp._web_state.searcher = Searcher("http://searxng.test", client=client)
    return mcp


async def test_exactly_two_tools_with_fixed_signatures():
    async with Client(server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"web_search", "read_page"}
    s = tools["web_search"].input_schema
    assert s["required"] == ["query"] and s["properties"]["max_results"]["default"] == 6
    r = tools["read_page"].input_schema
    assert r["required"] == ["url"] and r["properties"]["max_chars"]["default"] == 8000


async def test_web_search_call():
    async with Client(server()) as c:
        res = await c.call_tool("web_search", {"query": "x"})
    assert not res.is_error
    assert "provider: searxng" in res.content[0].text


async def test_read_page_refuses_local_targets():
    async with Client(server()) as c:
        for url in ["http://127.0.0.1:8888/search?q=a", "http://searxng:8080/", "file:///etc/passwd", "http://169.254.169.254/"]:
            res = await c.call_tool("read_page", {"url": url})
            assert res.is_error, url
            assert "Refused" in res.content[0].text or "Blocked" in res.content[0].text
