"""Installed models, catalog, Hugging Face browsing and downloads."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..models.downloads import DownloadError
from .deps import AppState, require_auth

router = APIRouter(prefix="/v1")


@router.get("/models")
def installed(category: str | None = None, st: AppState = Depends(require_auth)):
    items = st.registry.list()
    loaded_llm = st.llama.status().get("model_id")
    loaded_worker = {m["key"].split(":", 1)[1] for m in st.jobs.supervisor.last_status.get("loaded", [])}
    for m in items:
        m["loaded"] = m["id"] == loaded_llm or m["id"] in loaded_worker
    return [m for m in items if not category or m["category"] == category]


@router.post("/models/{model_id:path}/unload")
async def unload_model(model_id: str, st: AppState = Depends(require_auth)):
    """Frees the memory of any loaded model: the chat model, or an image/video/other model held by the GPU
    worker (the worker itself keeps running until its idle timeout)."""
    if st.llama.status().get("model_id") == model_id:
        if st.llama.active_streams:
            raise HTTPException(409, "the model is generating a response")
        await asyncio.to_thread(st.llama.unload)
        st.bus.publish("llm.status", st.llama.status())
    else:
        sup = st.jobs.supervisor
        keys = [m["key"] for m in sup.last_status.get("loaded", []) if m["key"].split(":", 1)[-1] == model_id]
        if keys:
            if sup.busy:
                raise HTTPException(409, "an image or video job is running; wait for it or cancel it first")
            for key in keys:
                await asyncio.to_thread(sup.release, key, 60.0)
    st.bus.publish("models.changed", {})
    return {"ok": True}


@router.post("/models/rescan")
async def rescan(st: AppState = Depends(require_auth)):
    items = await asyncio.to_thread(st.registry.scan)
    st.bus.publish("models.changed", {})
    return items


@router.delete("/models/{model_id:path}")
def delete_model(model_id: str, st: AppState = Depends(require_auth)):
    if st.llama.status().get("model_id") == model_id:
        st.llama.unload()
    try:
        st.models.delete(model_id)
    except FileNotFoundError:
        raise HTTPException(404, "model not found")
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    st.bus.publish("models.changed", {})
    return {"ok": True}


@router.get("/models/catalog")
def catalog(st: AppState = Depends(require_auth)):
    return st.models.catalog()


@router.get("/models/presets")
def presets(st: AppState = Depends(require_auth)):
    return st.models.presets()


@router.get("/models/disk")
def disk(st: AppState = Depends(require_auth)):
    return st.models.disk_usage()


@router.post("/models/install")
async def install(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    try:
        if body.get("catalog_id"):
            return await asyncio.to_thread(st.models.install_catalog, body["catalog_id"], body.get("quant"))
        if body.get("repo"):
            return await asyncio.to_thread(st.models.install_hf, body["repo"], body.get("files") or [],
                                           body.get("category") or "", body.get("name"))
    except DownloadError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "provide catalog_id or repo")


@router.get("/downloads")
def downloads(st: AppState = Depends(require_auth)):
    return st.downloads.list()


@router.post("/downloads/{did}/{action}")
async def download_action(did: str, action: str, st: AppState = Depends(require_auth)):
    if not st.downloads.get(did):
        raise HTTPException(404, "download not found")
    if action == "pause":
        st.downloads.pause(did)
    elif action == "resume":
        st.downloads.resume(did)
    elif action == "cancel":
        await asyncio.to_thread(st.downloads.cancel, did)
    elif action == "dismiss":
        st.db.execute("DELETE FROM downloads WHERE id = ? AND status IN ('completed','cancelled','failed')", [did])
    else:
        raise HTTPException(400, "unknown action")
    return st.downloads.get(did) or {"id": did, "status": "dismissed"}


@router.get("/hf/search")
async def hf_search(q: str, kind: str | None = None, st: AppState = Depends(require_auth)):
    try:
        return await asyncio.to_thread(st.models.hf_search, q, kind)
    except Exception as exc:
        raise HTTPException(502, f"Hugging Face search failed: {exc}")


@router.get("/hf/inspect")
async def hf_inspect(repo: str, st: AppState = Depends(require_auth)):
    if repo.count("/") != 1:
        raise HTTPException(400, "repo must look like 'owner/name'")
    try:
        return await asyncio.to_thread(st.models.hf_inspect, repo)
    except DownloadError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"could not read repository: {exc}")
