"""Web search status and a live connectivity test for the configured provider."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from .deps import AppState, require_auth

router = APIRouter(prefix="/v1/search")


@router.get("/status")
def search_status(st: AppState = Depends(require_auth)):
    return st.chat.websearch.status()


@router.post("/test")
async def search_test(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    """Runs one real query (no page fetching) so the reader can see whether the provider works."""
    query = (body.get("query") or "sarness").strip()[:200]
    ws = st.chat.websearch
    outcome = await ws.run(query, fetch=False)
    return {**ws.status(), "ok": outcome.usable, "error": outcome.error,
            "results": [{"title": s.title, "url": s.url} for s in outcome.sources]}
