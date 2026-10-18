"""Fetches third-party web pages without letting them reach the engine host's private network.

Every hop (including redirects) is resolved first; the request then goes to the vetted IP with the
original host in ``Host``/SNI, so DNS answers cannot change between the check and the connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 3
MAX_BYTES = 1_000_000
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
USER_AGENT = "Mozilla/5.0 (compatible; SarnessEngine/0.4; +https://github.com/sarja-technologies/Sarness)"


class FetchError(Exception):
    pass


def is_public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_loopback or a.is_link_local or a.is_private)


async def resolve_public(host: str, port: int) -> str:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    ips = [i[4][0] for i in infos]
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise FetchError("refusing to fetch a non-public address")
    return ips[0]


async def fetch_text(url: str, timeout: float, *, max_bytes: int = MAX_BYTES) -> tuple[str, str, str]:
    """Returns ``(final_url, content_type, body_text)`` for an http(s) page of a text type."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                                          # No compression: a small compressed chunk must not expand past the byte cap.
                                          "Accept-Encoding": "identity"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise FetchError("only http(s) pages can be fetched")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            ip = await resolve_public(parts.hostname, port)
            netloc = f"[{ip}]" if ":" in ip else ip
            target = parts._replace(netloc=f"{netloc}:{port}").geturl()
            req = client.build_request("GET", target, headers={"Host": parts.netloc},
                                       extensions={"sni_hostname": parts.hostname})
            resp = await client.send(req, stream=True)
            try:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise FetchError("redirect without a location")
                    url = urljoin(url, loc)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and ctype not in TEXT_TYPES:
                    raise FetchError(f"unsupported content type {ctype}")
                chunks, size = [], 0
                async for part in resp.aiter_raw():
                    size += len(part)
                    chunks.append(part)
                    if size >= max_bytes:
                        break
                raw = b"".join(chunks)[:max_bytes]
                charset = resp.charset_encoding or "utf-8"
                try:
                    return url, ctype or "text/html", raw.decode(charset, errors="replace")
                except LookupError:
                    return url, ctype or "text/html", raw.decode("utf-8", errors="replace")
            finally:
                await resp.aclose()
    raise FetchError("too many redirects")
