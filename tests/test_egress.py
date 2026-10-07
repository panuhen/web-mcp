import asyncio
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from web_mcp import egress, fetchers
from web_mcp.egress import BlockedURL, EgressProxy, blocked_reason, check_url
from web_mcp.fetchers import FetchError, HttpFetcher

BLOCKED = [
    "0.0.0.0", "0.1.2.3",
    "10.0.0.1", "10.255.255.255",
    "100.64.0.1", "100.127.255.254",          # CGNAT
    "127.0.0.1", "127.1.2.3",
    "169.254.169.254", "169.254.0.1",          # link-local / cloud metadata
    "172.16.0.1", "172.17.0.1", "172.31.255.255",  # private, Docker bridges
    "192.0.0.1", "192.0.2.1", "198.18.0.1", "198.51.100.7", "203.0.113.9",
    "192.168.0.1", "192.168.1.254",
    "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1", "2001:db8::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",                          # NAT64 to 127.0.0.1
    "2002:7f00:1::1",                           # 6to4 wrapping 127.0.0.1
]


@pytest.mark.parametrize("ip", BLOCKED)
def test_private_and_special_ranges_are_refused(ip):
    assert blocked_reason(ip) is not None


@pytest.mark.parametrize("ip", ["93.184.215.14", "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_pass(ip):
    assert blocked_reason(ip) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/html,hi",
        "http://",
        "http://user:pw@example.com/",
        "http://127.0.0.1:8888/search?q=x",
        "http://localhost:8888/",
        "http://foo.localhost/",
        "http://[::1]:8888/",
        "http://2130706433/",          # 127.0.0.1 as a decimal number
        "http://0x7f.1/",              # 127.0.0.1 in hex shorthand
        "http://0177.0.0.1/",          # octal
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:7f00:1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.17.0.1:8888/",
        "http://100.100.100.200/",
        "http://example.com\r\nHost: x/",
    ],
)
async def test_check_url_refuses(url):
    with pytest.raises(BlockedURL):
        await check_url(url)


async def test_name_resolving_to_private_is_refused(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BlockedURL):
        await check_url("https://rebind.example/")


async def test_mixed_public_and_private_answer_is_refused(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BlockedURL):
        await check_url("https://mixed.example/")


async def test_ipv4_is_preferred(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    checked = await check_url("https://dual.example/")
    assert checked.addresses == ["1.1.1.1", "2606:4700:4700::1111"]


# --- SOCKS egress proxy -----------------------------------------------------


async def socks_connect(port: int, host: str, dport: int) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    h = host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack("!H", dport))
    await writer.drain()
    reply = await reader.readexactly(10)
    writer.close()
    return reply[1]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254", "::1"])
async def test_egress_proxy_refuses_private_targets(host):
    proxy = EgressProxy()
    await proxy.start()
    try:
        code = await socks_connect(proxy.port, host, 8888)
        assert code == 2  # connection not allowed by ruleset
        assert proxy.refused == 1
    finally:
        await proxy.close()


# --- Redirects --------------------------------------------------------------


class _Redirector(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
            self.end_headers()
        else:
            body = b"internal secret"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


async def test_redirect_to_localhost_is_refused(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Redirector)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_port
    start = f"http://127.0.0.1:{port}/start"
    real_check = egress.check_url

    async def check_allowing_start(url):
        # Pretend only the first hop is a public site; every other URL gets the real check.
        if url == start:
            return egress.CheckedURL(url, "http", "127.0.0.1", port, ["127.0.0.1"])
        return await real_check(url)

    monkeypatch.setattr(fetchers, "check_url", check_allowing_start)
    try:
        f = HttpFetcher(None, 1_000_000)
        with pytest.raises(FetchError) as e:
            await f.fetch(start, 5)
        assert e.value.kind == "blocked-url"
    finally:
        srv.shutdown()
