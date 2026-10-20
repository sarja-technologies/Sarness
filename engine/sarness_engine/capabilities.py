"""What this engine can do, in one document, for multi-server desktops.

The desktop connects to several engines at once and routes each request to a server
that has the requested model. It reads this document (``GET /v1/capabilities``) to
learn the server's identity, hardware, free memory, backends, installed models with
the workloads they serve, and current load.
"""

from __future__ import annotations

import time
from typing import Any

from . import API_VERSION, __version__, hardware

# How many image/video jobs the engine runs at once (the GPU worker is single-job) and how many
# may wait in its queue before the desktop should prefer another server.
JOB_CONCURRENCY = 1
QUEUE_SOFT_LIMIT = 8

_CODE_HINTS = ("coder", "code", "devstral", "codestral")
_REASONING_HINTS = ("gpt-oss", "reason", "thinking", "-r1", "qwq", "deepseek")


def model_kinds(m: dict[str, Any]) -> list[str]:
    """Workloads a model serves: chat, code, vision, reasoning, image, video, upscale."""
    cat = m.get("category")
    caps = m.get("capabilities") or {}
    if cat == "llm":
        kinds = ["chat"]
        tags = {str(t).lower() for t in (m.get("tags") or [])}
        name = f"{m.get('id', '')} {m.get('name', '')}".lower()
        if tags & {"code", "coding", "agent"} or any(h in name for h in _CODE_HINTS):
            kinds.append("code")
        if caps.get("vision"):
            kinds.append("vision")
        if caps.get("reasoning") or "reasoning" in tags or any(h in name for h in _REASONING_HINTS):
            kinds.append("reasoning")
        return kinds
    return {"image": ["image"], "video": ["video"], "upscaler": ["upscale"]}.get(cat, [])


def _runtime(m: dict[str, Any]) -> str:
    if m.get("remote"):
        return "gateway"
    return {"gguf": "llama.cpp", "esrgan": "spandrel"}.get(m.get("family") or "", "diffusers")


def model_descriptor(m: dict[str, Any], loaded: set[str]) -> dict[str, Any]:
    caps = m.get("capabilities") or {}
    return {
        "id": m["id"],
        "name": m.get("name") or m["id"],
        "category": m.get("category"),
        "family": m.get("family"),
        "runtime": _runtime(m),
        "kinds": model_kinds(m),
        "modes": caps.get("modes") or [],
        "quantization": m.get("quantization"),
        "size_bytes": m.get("size"),
        "memory_mb": m.get("vram_mb"),
        "supported": m.get("supported", True),
        "loaded": m["id"] in loaded,
        # The chat reference the engine's chat and agent APIs accept for this model.
        "chat_ref": f"local:{m['id']}" if m.get("category") == "llm" else None,
    }


def capabilities(st: Any) -> dict[str, Any]:
    snap = hardware.system_snapshot(st.cfg.root)
    gpus = snap["gpus"]
    g = gpus[0] if gpus else None
    llm = st.llama.status()
    worker = st.jobs.supervisor
    # Worker cache keys look like "image:<model id>", "video:<model id>".
    loaded = {str(e.get("key", "")).split(":", 1)[-1] for e in (worker.last_status.get("loaded") or [])
              if isinstance(e, dict)}
    if llm.get("state") == "ready" and llm.get("model_id"):
        loaded.add(llm["model_id"])
    models = [model_descriptor(m, loaded) for m in st.registry.list()]
    counts = st.jobs.counts()
    backends = []
    if st.llama_runtime.binary():
        backends.append("llama.cpp")
    if _has_torch():
        backends.append("diffusers")
    if st.gateway and st.gateway.configured:
        backends.append("gateway")
    endpoints = [{"id": e.get("id"), "name": e.get("name")} for e in (st.settings.get("llm_endpoints") or [])]
    experiments = [e.id for e in st.experiments.list()] if st.experiments else []
    issues = []
    if not gpus:
        issues.append("no NVIDIA GPU detected")
    return {
        "server_id": st.cfg.server_id,
        "engine_version": __version__,
        "api_version": API_VERSION,
        "hostname": snap["hostname"],
        "arch": snap["arch"],
        "os": snap["distro"],
        "cpu": snap["cpu"]["name"],
        "gpu": None if g is None else {
            "name": g["name"],
            "compute_capability": g.get("compute_capability"),
            "unified_memory": g.get("unified_memory", False),
            "memory_total_mb": g["vram_total_mb"],
            "memory_free_mb": g["vram_free_mb"],
            "utilization_pct": g.get("utilization_pct"),
            "driver": g.get("driver_version"),
            "cuda": g.get("cuda_driver_version"),
        },
        "memory": snap["memory"],
        "storage_free_gb": snap["storage"]["free_gb"],
        "backends": backends,
        "experiments": experiments,
        "chat_endpoints": endpoints,
        "models": models,
        "llm": {"state": llm.get("state"), "model_id": llm.get("model_id"), "active_streams": llm.get("active_streams", 0)},
        "jobs": {
            "running": counts.get("running", 0),
            "queued": counts.get("queued", 0),
            "concurrency": JOB_CONCURRENCY,
            "queue_soft_limit": QUEUE_SOFT_LIMIT,
            "busy_reason": st.jobs.gpu_job_reason(),
        },
        "features": ["capabilities", "job_client_key"],
        "health": {"status": "degraded" if issues else "ok", "issues": issues},
        "generated_at": time.time(),
    }


def _has_torch() -> bool:
    import importlib.util

    return importlib.util.find_spec("torch") is not None
