"""Search backends. Each returns real results from a real service or raises :class:`SearchUnavailable`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .safe_http import USER_AGENT


class SearchUnavailable(Exception):
    """The provider could not produce results (network, rate limit, bad credentials, no configuration)."""


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _http_url(u: str) -> bool:
    return urlsplit(u).scheme in ("http", "https")


async def searxng(client: httpx.AsyncClient, base_url: str, query: str, limit: int) -> list[Hit]:
    if not base_url:
        raise SearchUnavailable("no SearXNG URL is configured")
    try:
        r = await client.get(base_url.rstrip("/") + "/search", params={"q": query, "format": "json", "safesearch": 1})
    except httpx.HTTPError as exc:
        raise SearchUnavailable(f"SearXNG is unreachable ({type(exc).__name__})") from exc
    if r.status_code == 403:
        raise SearchUnavailable("SearXNG refused the request: enable the JSON format in its settings (search.formats)")
    if r.status_code != 200:
        raise SearchUnavailable(f"SearXNG returned HTTP {r.status_code}")
    try:
        results = r.json().get("results") or []
    except ValueError as exc:
        raise SearchUnavailable("SearXNG did not return JSON") from exc
    return [Hit(_clean(x.get("title")), x["url"], _clean(x.get("content"))) for x in results
            if isinstance(x, dict) and _http_url(x.get("url") or "")][:limit]


async def brave(client: httpx.AsyncClient, key: str, query: str, limit: int) -> list[Hit]:
    if not key:
        raise SearchUnavailable("no Brave Search API key is configured")
    try:
        r = await client.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit},
                             headers={"X-Subscription-Token": key, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SearchUnavailable(f"Brave Search is unreachable ({type(exc).__name__})") from exc
    if r.status_code in (401, 403):
        raise SearchUnavailable("Brave Search rejected the API key")
    if r.status_code == 429:
        raise SearchUnavailable("Brave Search rate limit reached")
    if r.status_code != 200:
        raise SearchUnavailable(f"Brave Search returned HTTP {r.status_code}")
    results = ((r.json().get("web") or {}).get("results")) or []
    return [Hit(_clean(x.get("title")), x["url"], _clean(re.sub(r"<[^>]+>", "", x.get("description") or "")))
            for x in results if _http_url(x.get("url") or "")][:limit]


class _DDG(HTMLParser):
    """Parses the result list of DuckDuckGo's no-JavaScript page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[Hit] = []
        self._mode: str | None = None
        self._href = ""
        self._buf: list[str] = []
        self._title = ""
        self._pending: Hit | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._mode, self._href, self._buf = "title", a.get("href") or "", []
        elif tag in ("a", "div") and "result__snippet" in cls:
            self._mode, self._buf = "snippet", []

    def handle_endtag(self, tag):
        if tag == "a" and self._mode == "title":
            self._title = _clean("".join(self._buf))
            url = self._href
            if url.startswith("//"):
                url = "https:" + url
            qs = parse_qs(urlsplit(url).query)
            if "uddg" in qs:
                url = unquote(qs["uddg"][0])
            if self._title and _http_url(url) and "duckduckgo.com/y.js" not in url:
                self._pending = Hit(self._title, url, "")
                self.hits.append(self._pending)
            else:
                self._pending = None
            self._mode = None
        elif self._mode == "snippet" and tag in ("a", "div"):
            if self._pending is not None:
                self._pending.snippet = _clean("".join(self._buf))
            self._mode = None

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


async def duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> list[Hit]:
    try:
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "wt-wt"},
                              headers={"User-Agent": USER_AGENT, "Referer": "https://html.duckduckgo.com/"})
    except httpx.HTTPError as exc:
        raise SearchUnavailable(f"DuckDuckGo is unreachable ({type(exc).__name__})") from exc
    if r.status_code != 200 or "anomaly" in r.text[:4000].lower():
        raise SearchUnavailable("DuckDuckGo declined the request (rate limited); try again later or configure SearXNG")
    p = _DDG()
    p.feed(r.text)
    return p.hits[:limit]
