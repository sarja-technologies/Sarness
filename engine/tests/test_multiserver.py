"""Multi-server support: capabilities document, server identity, idempotent job submission,
unified-memory GPUs (DGX Spark GB10) and ARM64 llama.cpp runtime selection. No GPU needed."""

import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sarness_engine import capabilities as caps_mod
from sarness_engine import hardware
from sarness_engine.app import create_app
from sarness_engine.config import load_config, save_config
from sarness_engine.db import MIGRATIONS, Database
from sarness_engine.runtime import llama


def _install_fake_sdxl(models_dir):
    d = models_dir / "image" / "my-sdxl"
    (d / "unet").mkdir(parents=True)
    (d / "model_index.json").write_text('{"_class_name": "StableDiffusionXLPipeline"}')
    (d / "unet" / "diffusion_pytorch_model.safetensors").write_bytes(b"\0" * 64)


@pytest.fixture
def client(tmp_path):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    save_config(cfg)
    cfg.ensure_dirs()
    _install_fake_sdxl(cfg.models_dir)
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        c.cfg = cfg
        yield c


def test_capabilities_document(client):
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    d = r.json()
    assert d["server_id"].startswith("srv_")
    assert d["arch"] in ("x86_64", "aarch64")
    assert {"running", "queued", "concurrency", "queue_soft_limit"} <= set(d["jobs"])
    assert d["health"]["status"] in ("ok", "degraded")
    sdxl = next(m for m in d["models"] if m["id"] == "image:my-sdxl")
    assert sdxl["kinds"] == ["image"] and "txt2img" in sdxl["modes"]
    assert sdxl["runtime"] == "diffusers" and sdxl["chat_ref"] is None and sdxl["loaded"] is False
    assert client.get("/v1/capabilities", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_server_id_is_stable_across_restarts(tmp_path):
    cfg = load_config(tmp_path / "d")
    cfg.rotate_token()
    save_config(cfg)
    with TestClient(create_app(cfg, start_background=False)):
        pass
    first = load_config(tmp_path / "d").server_id
    assert first.startswith("srv_")
    cfg2 = load_config(tmp_path / "d")
    with TestClient(create_app(cfg2, start_background=False)):
        pass
    assert load_config(tmp_path / "d").server_id == first


def test_job_submission_is_idempotent_with_client_key(client):
    body = {"kind": "image", "simple": {"model": "image:my-sdxl", "prompt": "a lighthouse", "steps": 4},
            "client_key": "desk-1-abcdef123"}
    a = client.post("/v1/jobs", json=body)
    assert a.status_code == 200, a.text
    b = client.post("/v1/jobs", json=body)
    assert b.json()["id"] == a.json()["id"], "a retried submission returns the original job"
    other = client.post("/v1/jobs", json={**body, "client_key": "desk-1-abcdef124"})
    assert other.json()["id"] != a.json()["id"]
    plain1 = client.post("/v1/jobs", json={k: v for k, v in body.items() if k != "client_key"})
    plain2 = client.post("/v1/jobs", json={k: v for k, v in body.items() if k != "client_key"})
    assert plain1.json()["id"] != plain2.json()["id"], "without a key every request is a new job"
    assert client.post("/v1/jobs", json={**body, "client_key": "short"}).status_code == 400
    queued = [j for j in client.get("/v1/jobs").json() if j["status"] == "queued"]
    assert len(queued) == 4


def test_concurrent_duplicate_submissions_create_one_job(client):
    body = {"kind": "image", "simple": {"model": "image:my-sdxl", "prompt": "race"}, "client_key": "race-key-000001"}
    ids, errors = [], []

    def go():
        try:
            ids.append(client.post("/v1/jobs", json=body).json()["id"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(ids)) == 1


def test_migration_v3_adds_client_key_and_keeps_jobs(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for idx, script in enumerate(MIGRATIONS[:2], start=1):
        conn.executescript("BEGIN;" + script + f"; PRAGMA user_version={idx}; COMMIT;")
    conn.execute("INSERT INTO jobs (id, kind, status, workflow, created_at) VALUES ('j_old', 'image', 'completed', '{}', 1)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version == len(MIGRATIONS) >= 3
    assert db.one("SELECT id, client_key FROM jobs WHERE id = 'j_old'") == {"id": "j_old", "client_key": None}


def test_model_kinds():
    k = caps_mod.model_kinds
    assert k({"category": "llm", "id": "qwen3-coder-next@Q4_K_M", "name": "Qwen3 Coder Next"}) == ["chat", "code"]
    assert k({"category": "llm", "id": "gpt-oss-120b@MXFP4", "name": "GPT-OSS 120B"}) == ["chat", "reasoning"]
    assert k({"category": "llm", "id": "gemma", "capabilities": {"vision": True}}) == ["chat", "vision"]
    assert k({"category": "video"}) == ["video"]
    assert k({"category": "lora"}) == []


class _FakeNvml:
    NVML_TEMPERATURE_GPU = 0

    class NVMLError(Exception):
        pass

    def __init__(self, unified):
        self.unified = unified

    def nvmlSystemGetDriverVersion(self):
        return "580.178.04"

    def nvmlSystemGetCudaDriverVersion_v2(self):
        return 13000

    def nvmlDeviceGetCount(self):
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):
        return i

    def nvmlDeviceGetMemoryInfo(self, h):
        if self.unified:
            raise self.NVMLError("Not Supported")
        return SimpleNamespace(total=12 << 30, used=1 << 30, free=11 << 30)

    def nvmlDeviceGetUtilizationRates(self, h):
        return SimpleNamespace(gpu=7)

    def nvmlDeviceGetCudaComputeCapability(self, h):
        return (12, 1) if self.unified else (8, 9)

    def nvmlDeviceGetTemperature(self, h, s):
        return 40

    def nvmlDeviceGetPowerUsage(self, h):
        raise self.NVMLError("Not Supported")

    def nvmlDeviceGetComputeRunningProcesses(self, h):
        return []

    def nvmlDeviceGetName(self, h):
        return "NVIDIA GB10" if self.unified else "NVIDIA GeForce RTX 4070 Ti"


def test_unified_memory_gpu_reports_system_memory(monkeypatch):
    monkeypatch.setattr(hardware, "_NVML", True)
    monkeypatch.setattr(hardware, "pynvml", _FakeNvml(unified=True))
    g = hardware.gpus()
    assert len(g) == 1, "a NOT_SUPPORTED memory query must not hide the GPU"
    assert g[0]["unified_memory"] is True and g[0]["compute_capability"] == "12.1"
    assert g[0]["vram_total_mb"] > 0 and g[0]["vram_free_mb"] <= g[0]["vram_total_mb"]
    assert g[0]["power_w"] is None

    monkeypatch.setattr(hardware, "pynvml", _FakeNvml(unified=False))
    g = hardware.gpus()
    assert g[0]["unified_memory"] is False and g[0]["vram_total_mb"] == 12288 and g[0]["utilization_pct"] == 7


def test_llama_variant_and_cuda_arch(monkeypatch):
    gpu = [{"compute_capability": "12.1"}]
    monkeypatch.setattr(llama, "gpus", lambda: gpu)
    monkeypatch.setattr(llama, "arch", lambda: "aarch64")
    monkeypatch.setattr(llama, "find_nvcc", lambda: "/usr/local/cuda/bin/nvcc")
    assert llama.LlamaRuntime.choose_variant() == "cuda-src"
    assert llama.cuda_architecture() == "121a-real"
    monkeypatch.setattr(llama, "find_nvcc", lambda: None)
    assert llama.LlamaRuntime.choose_variant() == "cpu-src"
    monkeypatch.setattr(llama, "arch", lambda: "x86_64")
    assert llama.LlamaRuntime.choose_variant() == "cuda-12.8", "x86-64 keeps the prebuilt CUDA release"
    gpu[0]["compute_capability"] = "8.9"
    assert llama.cuda_architecture() == "89-real"
    with pytest.raises(ValueError):
        llama.LlamaRuntime(__import__("pathlib").Path("/nonexistent")).install(variant="bogus")


def test_hardware_arch_is_normalised(monkeypatch):
    monkeypatch.setattr(hardware.platform, "machine", lambda: "arm64")
    assert hardware.arch() == "aarch64"
    monkeypatch.setattr(hardware.platform, "machine", lambda: "AMD64")
    assert hardware.arch() == "x86_64"
