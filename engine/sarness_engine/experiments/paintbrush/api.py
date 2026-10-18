"""``/v1/experiments/gods-paintbrush/…`` routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from ...api.deps import AppState, require_auth
from ..scheduler import RequestCancelled, Superseded
from .masks import MaskError
from .service import EXPERIMENT_ID, Paintbrush, PaintbrushError

router = APIRouter(prefix=f"/v1/experiments/{EXPERIMENT_ID}")


def _svc(st: AppState) -> Paintbrush:
    exp = st.experiments.get(EXPERIMENT_ID) if st.experiments else None
    if exp is None:
        raise HTTPException(404, "God's Paintbrush is not available on this engine")
    return exp  # type: ignore[return-value]


async def _guard(coro):
    try:
        return await coro
    except MaskError as exc:
        raise HTTPException(422, str(exc)) from None
    except PaintbrushError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except Superseded:
        return JSONResponse({"detail": "superseded by a newer request", "superseded": True}, status_code=409)
    except RequestCancelled:
        return JSONResponse({"detail": "cancelled", "cancelled": True}, status_code=409)


async def _sync(fn, *args):
    return await asyncio.to_thread(fn, *args)


@router.get("/status")
async def status(st: AppState = Depends(require_auth)):
    return await asyncio.to_thread(_svc(st).status)


@router.post("/render")
async def render(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return await _guard(_svc(st).render(body))


@router.post("/cancel")
async def cancel(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return {"cancelled": _svc(st).cancel(str(body.get("session") or "default"))}


@router.post("/warmup")
async def warmup(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return await _guard(_svc(st).warmup(bool(body.get("refine"))))


@router.post("/release")
async def release(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return await _guard(_svc(st).release(str(body.get("level") or "all")))


@router.post("/install")
def install(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    try:
        return _svc(st).installer.start(body.get("components"))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/install/cancel")
def install_cancel(st: AppState = Depends(require_auth)):
    _svc(st).installer.cancel()
    return {"ok": True}


@router.post("/renders/{render_id}/save")
async def save_render(render_id: str, body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    return await _guard(_sync(_svc(st).save_render, render_id, body))


@router.post("/renders/{render_id}/export")
async def export_render(render_id: str, st: AppState = Depends(require_auth)):
    return await _guard(_sync(_svc(st).export_render, render_id))


@router.post("/mask/export")
async def export_mask(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return await _guard(_sync(_svc(st).export_mask, body.get("mask"), str(body.get("format") or "coco")))
