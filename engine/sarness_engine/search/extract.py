"""Plain-text extraction from HTML using only the standard library."""

from __future__ import annotations

import re
from html.parser import HTMLParser

SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe", "template",
        "button", "select", "canvas", "figure"}
BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main", "blockquote", "pre",
         "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt"}
MIN_LINE = 25


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lines: list[str] = []
        self.main_lines: list[str] = []  # text inside <main>/<article>, preferred when present
        self._main = 0
        self._cur: list[str] = []
        self._skip = 0
        self._in_title = False

    def _flush(self) -> None:
        line = re.sub(r"\s+", " ", "".join(self._cur)).strip()
        self._cur = []
        if line:
            self.lines.append(line)
            if self._main:
                self.main_lines.append(line)

    def handle_starttag(self, tag, attrs):
        if tag in ("main", "article"):
            self._flush()
            self._main += 1
        if tag == "title":
            self._in_title = True
        if tag in SKIP:
            self._skip += 1
        elif tag in BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("main", "article"):
            self._flush()
            self._main = max(0, self._main - 1)
        if tag == "title":
            self._in_title = False
        if tag in SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._cur.append(data)


def extract(html: str, limit: int) -> tuple[str, str]:
    """Returns ``(title, text)``; text keeps substantial lines in document order, capped at ``limit`` characters."""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup must never break a chat
        pass
    p._flush()
    seen: set[str] = set()
    out: list[str] = []
    total = 0
    lines = p.main_lines if sum(map(len, p.main_lines)) >= 200 else p.lines
    for line in lines:
        if len(line) < MIN_LINE or line in seen:
            continue
        seen.add(line)
        out.append(line)
        total += len(line) + 1
        if total >= limit:
            break
    text = "\n".join(out)
    return re.sub(r"\s+", " ", p.title).strip(), text[:limit]
