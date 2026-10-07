"""SSRF guard.

Two layers:

1. `check_url()` validates a URL before any fetch and before following each
   redirect: http/https only, the host must resolve, and every address must be
   public. It gives clear error messages.
2. `EgressProxy` is a tiny SOCKS5 server on loopback that both fetchers (curl_cffi
   and the browser) send all their traffic through. It resolves names itself,
   refuses any non-public address and connects only to the address it checked,
   so DNS rebinding, redirects inside the browser and page scripts that try to
   reach the LAN, the Docker network or loopback are all refused at connect time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("web_mcp.egress")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Ranges refused explicitly. `is_global` already covers most of them; listing
# them keeps the intent readable and guards against stdlib changes.
_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",          # "this" network
        "10.0.0.0/8",         # private
        "100.64.0.0/10",      # CGNAT
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local, cloud metadata 169.254.169.254
        "172.16.0.0/12",      # private (Docker bridges live here)
        "192.0.0.0/24",       # IETF protocol assignments
        "192.0.2.0/24",       # TEST-NET-1
        "192.88.99.0/24",     # 6to4 relay anycast
        "192.168.0.0/16",     # private
        "198.18.0.0/15",      # benchmarking
        "198.51.100.0/24",    # TEST-NET-2
        "203.0.113.0/24",     # TEST-NET-3
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",      # IPv4-mapped (checked again via the embedded v4)
        "64:ff9b::/96",       # NAT64 (checked again via the embedded v4)
        "64:ff9b:1::/48",
        "100::/64",           # discard
        "2001:db8::/32",      # documentation
        "fc00::/7",           # unique local (incl. fd00:ec2::254 metadata)
        "fe80::/10",          # link-local
        "fec0::/10",          # old site-local
        "ff00::/8",           # multicast
    )
]

ALLOWED_SCHEMES = {"http", "https"}


class BlockedURL(Exception):
    """The URL is not allowed (scheme, host or address)."""


def blocked_reason(ip: IPAddress | str) -> str | None:
    """Return why an address is refused, or None when it is a public address."""
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return "not an IP address"
    if isinstance(addr, ipaddress.IPv6Address):
        # Embedded IPv4 forms: check the IPv4 address they really reach.
        if addr.ipv4_mapped is not None:
            return blocked_reason(addr.ipv4_mapped) or None
        if addr.sixtofour is not None:
            inner = blocked_reason(addr.sixtofour)
            if inner:
                return f"6to4 to {inner}"
        if addr.teredo is not None:
            return "teredo address"
        if addr in ipaddress.ip_network("64:ff9b::/96"):
            inner = ipaddress.IPv4Address(addr.packed[-4:])
            r = blocked_reason(inner)
            return f"NAT64 to {r}" if r else None
        if addr.scope_id:
            return "scoped address"
    for net in _BLOCKED_NETS:
        if addr.version == net.version and addr in net:
            return f"non-public address range {net}"
    if not addr.is_global:
        return "non-public address"
    return None


@dataclass
class CheckedURL:
    url: str
    scheme: str
    host: str
    port: int
    addresses: list[str]


def _split(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url.strip():
        raise BlockedURL("empty URL")
    if any(c in url for c in "\r\n\t\x00"):
        raise BlockedURL("URL contains control characters")
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(f"only http and https URLs are allowed (got {scheme or 'no scheme'!r})")
    if parts.username or parts.password:
        raise BlockedURL("URLs with credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise BlockedURL("invalid port") from None
    return scheme, host, port


async def resolve_public(host: str, port: int) -> list[str]:
    """Resolve a host and return its public addresses. Raise BlockedURL when none are public."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        reason = blocked_reason(literal)
        if reason:
            raise BlockedURL(f"address refused: {reason}")
        return [str(literal)]
    if host == "localhost" or host.endswith(".localhost"):
        raise BlockedURL("address refused: localhost")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise BlockedURL("host name does not resolve") from None
    addrs: list[str] = []
    refused: list[str] = []
    for family, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        reason = blocked_reason(ip)
        if reason:
            refused.append(reason)
        elif ip not in addrs:
            addrs.append(ip)
    if refused:
        # A name that points at both public and private addresses is suspicious
        # (or a rebinding setup). Refuse it outright.
        raise BlockedURL(f"address refused: {refused[0]}")
    if not addrs:
        raise BlockedURL("host name does not resolve")
    # IPv4 first: many home and container networks have no working IPv6 route.
    addrs.sort(key=lambda a: ":" in a)
    return addrs


async def check_url(url: str) -> CheckedURL:
    scheme, host, port = _split(url)
    addrs = await resolve_public(host, port)
    return CheckedURL(url=url.strip(), scheme=scheme, host=host, port=port, addresses=addrs)


# ---------------------------------------------------------------------------
# SOCKS5 egress proxy


class EgressProxy:
    """Loopback SOCKS5 (no auth, CONNECT only) that refuses non-public targets."""

    def __init__(self, connect_timeout: float = 5.0):
        self.connect_timeout = connect_timeout
        self._server: asyncio.base_events.Server | None = None
        self.port: int = 0
        self.refused = 0
        self.connections = 0

    @property
    def url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"

    @property
    def browser_server(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readexactly(2), 10)
            if head[0] != 5:
                return
            methods = await reader.readexactly(head[1])
            if 0 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return
            writer.write(b"\x05\x00")
            await writer.drain()

            ver, cmd, _, atyp = await asyncio.wait_for(reader.readexactly(4), 10)
            if atyp == 1:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            elif atyp == 3:
                n = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(n)).decode("ascii", errors="replace")
            else:
                await self._reply(writer, 8)
                return
            port = struct.unpack("!H", await reader.readexactly(2))[0]
            if cmd != 1:  # CONNECT only
                await self._reply(writer, 7)
                return
            try:
                addrs = await resolve_public(host.rstrip(".").lower(), port)
            except BlockedURL:
                self.refused += 1
                log.debug("egress refused")
                await self._reply(writer, 2)  # not allowed by ruleset
                return
            upstream_reader = None
            for ip in addrs[:4]:
                try:
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port), self.connect_timeout
                    )
                    break
                except (OSError, asyncio.TimeoutError):
                    continue
            if upstream_reader is None:
                await self._reply(writer, 5)  # connection refused
                return
            self.connections += 1
            await self._reply(writer, 0)
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(upstream_reader, writer),
                return_exceptions=True,
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception:  # never let a bad client crash the loop's callback
            log.debug("egress handler error", exc_info=True)
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
        await writer.drain()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except Exception:
                pass
