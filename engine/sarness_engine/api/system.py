"""Health, system information, settings and runtime management."""

from __future__ import annotations

import asyncio
import importlib.metadata as md
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import API_VERSION, __version__, hardware
from ..runtime.llama import VARIANTS
from .deps import AppState, require_auth

router = APIRouter(prefix="/v1")


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Unauthenticated liveness probe. Reveals nothing about the machine."""
    return {"status": "ok", "service": "sarness-engine", "api_version": API_VERSION, "engine_version": __version__,
            "auth": "bearer"}


def _pkg(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


@router.get("/system")
def system(st: AppState = Depends(require_auth)) -> dict[str, Any]:
    snap = hardware.system_snapshot(st.cfg.root)
    torch_v = _pkg("torch")
    worker = st.jobs.supervisor
    llm = st.llama.status()
    issues = []
    if not snap["gpus"]:
        issues.append("No NVIDIA GPU detected — generation will run on the CPU and be extremely slow.")
    if not torch_v:
        issues.append("PyTorch is not installed; image and video generation are unavailable.")
    if not st.llama_runtime.binary():
        issues.append("llama.cpp runtime is not installed; local chat is unavailable until it is installed.")
    if snap["storage"]["free_gb"] < 10:
        issues.append(f"Low disk space: {snap['storage']['free_gb']} GB free.")
    if llm["state"] == "error":
        issues.append(f"Chat model error: {llm['error']}")
    models = st.registry.list()
    return {
        "engine": {"version": __version__, "api_version": API_VERSION, "uptime_s": time.time() - st.started_at,
                   "data_dir": str(st.cfg.root), "outputs_dir": str(st.cfg.outputs_dir),
                   "models_dir": str(st.cfg.models_dir), "bind": f"{st.cfg.host}:{st.cfg.port}"},
        "hardware": snap,
        "runtime": {
            "torch": torch_v, "torch_cuda": _cuda_tag(torch_v),
            "diffusers": _pkg("diffusers"), "transformers": _pkg("transformers"),
            "cuda_driver": snap["gpus"][0]["cuda_driver_version"] if snap["gpus"] else None,
            "llama_cpp": st.llama_runtime.version(),
            "worker": {"alive": worker.alive, "busy": worker.busy, "info": worker.info,
                       "loaded": worker.last_status.get("loaded", []),
                       "vram_allocated_mb": worker.last_status.get("torch_vram_allocated_mb"),
                       "vram_reserved_mb": worker.last_status.get("torch_vram_reserved_mb")},
            "experiments": [{"id": e.id, "gpu_active": e.gpu_active()} for e in (st.experiments.list() if st.experiments else [])],
        },
        "llm": llm,
        "jobs": st.jobs.counts(),
        "models": {"installed": len(models), "by_category": _count(models, "category")},
        "health": {"status": "degraded" if issues else "ok", "issues": issues},
    }


@router.get("/capabilities")
def capabilities(st: AppState = Depends(require_auth)) -> dict[str, Any]:
    """Identity, hardware, free memory, backends, models with their workloads, and load:
    everything a multi-server desktop needs to route requests to this engine."""
    from ..capabilities import capabilities as build

    return build(st)


def _cuda_tag(torch_v: str | None) -> str | None:
    if not torch_v or "+cu" not in torch_v:
        return None
    digits = torch_v.split("+cu", 1)[1]
    return f"{digits[:-1]}.{digits[-1]}" if digits.isdigit() and len(digits) >= 2 else digits


def _count(items, key):
    out: dict[str, int] = {}
    for i in items:
        out[i[key]] = out.get(i[key], 0) + 1
    return out


# ------------------------------------------------------------------ settings
@router.get("/settings")
def get_settings(st: AppState = Depends(require_auth)):
    return st.settings.all_public()


@router.patch("/settings")
def patch_settings(patch: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    try:
        out = st.settings.update(patch)
    except (KeyError, TypeError) as exc:
        raise HTTPException(400, f"invalid setting: {exc}")
    if "extra_model_dirs" in patch:
        from pathlib import Path

        st.registry.extra_dirs = [Path(d).expanduser() for d in patch["extra_model_dirs"]]
        st.registry.scan()
    if "gateway_url" in patch or "gateway_token" in patch:
        st.registry.scan()
        st.bus.publish("models.changed", {})
    st.bus.publish("settings.changed", out)
    return out


@router.get("/gateway")
def gateway_status(st: AppState = Depends(require_auth)):
    gw = st.gateway
    return {"configured": gw.configured, "url": gw.base_url or None, "error": gw.last_error,
            "models": [m["id"] for m in gw.last_models]}


@router.post("/gateway/refresh")
async def gateway_refresh(st: AppState = Depends(require_auth)):
    await asyncio.to_thread(st.registry.scan)
    st.bus.publish("models.changed", {})
    return gateway_status(st)


# ------------------------------------------------------------------ runtimes
_install_state: dict[str, Any] = {"running": False, "error": None, "done": 0, "total": 0}


@router.get("/runtimes")
def runtimes(st: AppState = Depends(require_auth)):
    return {
        "llama_cpp": {"installed": bool(st.llama_runtime.binary()), "version": st.llama_runtime.version(),
                      "path": str(st.llama_runtime.binary() or ""), "suggested_variant": st.llama_runtime.choose_variant(),
                      "install": _install_state},
        "torch": _pkg("torch"), "diffusers": _pkg("diffusers"), "transformers": _pkg("transformers"),
        "spandrel": _pkg("spandrel"), "imageio_ffmpeg": _pkg("imageio-ffmpeg"),
    }


@router.post("/runtimes/llama/install")
def install_llama(body: dict[str, Any] = Body(default={}), st: AppState = Depends(require_auth)):
    if _install_state["running"]:
        return _install_state
    variant = body.get("variant")
    if variant is not None and variant not in VARIANTS:
        raise HTTPException(400, f"variant must be one of {', '.join(VARIANTS)}")

    def progress(done, total):
        _install_state.update(done=done, total=total)
        st.bus.publish("runtime.progress", {"runtime": "llama_cpp", "done": done, "total": total})

    def run():
        _install_state.update(running=True, error=None, done=0, total=0)
        try:
            if st.llama.status()["state"] != "stopped":
                st.llama.unload()
            version = st.llama_runtime.install(progress, variant)
            st.bus.publish("runtime.installed", {"runtime": "llama_cpp", "version": version})
        except Exception as exc:  # noqa: BLE001
            _install_state["error"] = str(exc)
            st.bus.publish("runtime.failed", {"runtime": "llama_cpp", "error": str(exc)})
        finally:
            _install_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {**_install_state, "running": True}


# -------------------------------------------------------------------- worker
@router.post("/worker/release")
def worker_release(st: AppState = Depends(require_auth)):
    if st.jobs.supervisor.busy:
        raise HTTPException(409, "the GPU worker is running a job")
    st.jobs.supervisor.release()
    return {"ok": True}


@router.post("/worker/stop")
async def worker_stop(st: AppState = Depends(require_auth)):
    if st.jobs.supervisor.busy:
        raise HTTPException(409, "the GPU worker is running a job")
    await asyncio.to_thread(st.jobs.supervisor.stop)
    return {"ok": True}


# ----------------------------------------------------------------------- LLM
@router.get("/llm/status")
def llm_status(st: AppState = Depends(require_auth)):
    return st.llama.status()


@router.get("/llm/models")
def llm_models(st: AppState = Depends(require_auth)):
    return st.chat.available_models()


@router.post("/llm/load")
async def llm_load(body: dict[str, Any] = Body(...), st: AppState = Depends(require_auth)):
    return await st.chat.ensure_local(body["model_id"], body.get("ctx"), body.get("gpu_layers"))


@router.post("/llm/unload")
async def llm_unload(st: AppState = Depends(require_auth)):
    if st.llama.active_streams:
        raise HTTPException(409, "the model is generating a response")
    await asyncio.to_thread(st.llama.unload)
    s = st.llama.status()
    st.bus.publish("llm.status", s)
    return s


@router.post("/llm/endpoints/{endpoint_id}/refresh")
async def refresh_endpoint(endpoint_id: str, st: AppState = Depends(require_auth)):
    try:
        return {"models": await st.chat.refresh_endpoint_models(endpoint_id)}
    except Exception as exc:
        raise HTTPException(502, f"could not list models from endpoint: {exc}")
