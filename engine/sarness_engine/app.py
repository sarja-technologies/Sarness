"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import API_VERSION, __version__, hardware
from .api.deps import AppState
from .chat.service import ChatError, ChatService
from .config import EngineConfig
from .db import Database
from .experiments import build_registry
from .experiments import routers as experiment_routers
from .experiments.documents import Documents
from .events import EventBus
from .jobs.service import JobError, JobService
from .models.downloads import DownloadError, DownloadManager
from .models.registry import ModelRegistry
from .models.service import ModelService
from .remote.gateway import Gateway
from .runtime.llama import LlamaRuntime, LlamaServer
from .settings import Settings
from .storage import Storage, StorageError

log = logging.getLogger("sarness.app")
access_log = logging.getLogger("sarness.access")

DEFAULT_CORS = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
                "http://localhost:1420", "http://127.0.0.1:1420"]


def create_app(cfg: EngineConfig, *, start_background: bool = True) -> FastAPI:
    cfg.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = _build_state(cfg)
        app.state.sarness = st
        st.bus.bind_loop(asyncio.get_running_loop())
        st.jobs.recover()
        st.downloads.recover()
        await asyncio.to_thread(st.registry.scan)
        tasks = []
        await st.experiments.startup()
        if start_background:
            st.jobs.start()
            tasks.append(asyncio.create_task(_stats_loop(st)))
            tasks.append(asyncio.create_task(_gateway_loop(st)))
        log.info("Sarness Engine %s ready (data: %s)", __version__, cfg.root)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await st.experiments.shutdown()
            await st.jobs.shutdown()
            await asyncio.to_thread(st.llama.unload)
            st.db.close()

    app = FastAPI(title="Sarness Engine API", version=API_VERSION, lifespan=lifespan,
                  docs_url="/v1/docs", openapi_url="/v1/openapi.json", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=DEFAULT_CORS + list(cfg.cors_origins or []),
                       allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _access(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        # Never log query strings: they may contain signatures or tokens.
        if not request.url.path.startswith("/v1/files"):
            access_log.info("%s %s %d %.0fms", request.method, request.url.path, response.status_code,
                            (time.perf_counter() - t0) * 1000)
        return response

    for exc_type, status in ((JobError, None), (ChatError, None), (DownloadError, 400), (StorageError, 400)):
        def _handler(request, exc, _status=status):
            code = _status or getattr(exc, "status", 400)
            body = {"detail": str(exc)}
            if getattr(exc, "issues", None):
                body["issues"] = exc.issues
            return JSONResponse(body, status_code=code)

        app.add_exception_handler(exc_type, _handler)

    from .api import chat, jobs, library, models, search, system, ws

    for r in (system.router, library.router, jobs.router, models.router, chat.router, chat.agent_router, search.router, ws.router,
              *experiment_routers()):
        app.include_router(r)
    return app


def _build_state(cfg: EngineConfig) -> AppState:
    db = Database(cfg.db_path)
    bus = EventBus()
    settings = Settings(db)
    storage = Storage(cfg, db)
    registry = ModelRegistry(db, cfg.models_dir, settings.get("extra_model_dirs"))
    gateway = Gateway(settings)
    registry.remote_provider = gateway.refresh_sync
    runtime = LlamaRuntime(cfg.bin_dir)
    llama = LlamaServer(runtime, cfg.logs_dir)

    def token() -> str:
        return settings.get("hf_token") or ""

    def on_download_complete(row):
        registry.scan()
        bus.publish("models.changed", {"model_id": row["model_id"]})

    downloads = DownloadManager(db, bus.publish, token, on_download_complete)
    model_service = ModelService(registry, downloads, cfg.models_dir, token)
    jobs = JobService(db, bus, settings, storage, registry, llama, worker_options={
        "logs_dir": str(cfg.logs_dir), "outputs_dir": str(cfg.outputs_dir),
        "headroom_mb": settings.get("vram_headroom_mb"),
    }, gateway=gateway)
    chat = ChatService(db, bus, settings, storage, llama, registry, jobs)
    st = AppState(cfg=cfg, db=db, bus=bus, settings=settings, storage=storage, registry=registry,
                  downloads=downloads, models=model_service, llama_runtime=runtime, llama=llama, jobs=jobs,
                  chat=chat, gateway=gateway, started_at=time.time(), experiment_docs=Documents(db))
    st.experiments = build_registry(st)
    jobs.experiments = st.experiments
    return st


async def _gateway_loop(st: AppState) -> None:
    """Re-reads the gateway inventory while it is unreachable or empty (e.g. it started after the engine)."""
    while True:
        await asyncio.sleep(60)
        gw = st.gateway
        if gw.configured and (gw.last_error or not gw.last_models):
            before = len(gw.last_models)
            await asyncio.to_thread(st.registry.scan)
            if len(gw.last_models) != before:
                st.bus.publish("models.changed", {})


async def _stats_loop(st: AppState) -> None:
    """Broadcast lightweight live stats only while someone is listening."""
    import psutil

    psutil.cpu_percent(interval=None)
    while True:
        await asyncio.sleep(2.0)
        if st.bus.subscriber_count == 0:
            continue
        try:
            g = await asyncio.to_thread(hardware.gpus)
            vm = psutil.virtual_memory()
            st.bus.publish("system.stats", {
                "gpus": [{k: x[k] for k in ("index", "vram_used_mb", "vram_free_mb", "vram_total_mb",
                                            "utilization_pct", "temperature_c", "power_w")} for x in g],
                "cpu_pct": psutil.cpu_percent(interval=None),
                "ram_used_mb": (vm.total - vm.available) // 2**20, "ram_total_mb": vm.total // 2**20,
                "worker_alive": st.jobs.supervisor.alive, "llm": st.llama.status()["state"],
            })
        except Exception:
            log.exception("stats loop")


def data_dir_of(app: FastAPI) -> Path:
    return app.state.sarness.cfg.root
