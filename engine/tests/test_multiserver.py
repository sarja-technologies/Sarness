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


# --------------------------------------------------------------- Spark model families
def _gguf(path):
    from test_core import _write_gguf

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_gguf(path)


def test_registry_new_families_and_nested_mtp(tmp_path):
    from sarness_engine.models.registry import ModelRegistry

    cfg = load_config(tmp_path / "data")
    cfg.ensure_dirs()
    m = cfg.models_dir
    q = m / "image" / "Qwen--Qwen-Image-2.1"
    q.mkdir(parents=True)
    (q / "model_index.json").write_text('{"_class_name": "QwenImage21Pipeline"}')
    ltx = m / "video" / "some-ltx"
    ltx.mkdir(parents=True)
    (ltx / "model_index.json").write_text('{"_class_name": "LTX2Pipeline"}')
    # Sharded quant in a sub-folder; MTP head at the repo root (unsloth layout).
    repo = m / "llm" / "unsloth--Qwen3.8-Flash-Next-GGUF"
    _gguf(repo / "UD-IQ4_XS" / "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf")
    _gguf(repo / "UD-IQ4_XS" / "Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf")
    (repo / "mtp-Qwen3.8-Flash-Next-Q8_0.gguf").write_bytes(b"GGUF")
    (m / "llm" / "mmproj-stray.gguf").write_bytes(b"GGUF")  # belongs to no model in that repo
    reg = ModelRegistry(Database(cfg.db_path), m)
    items = {i["id"]: i for i in reg.scan()}
    assert items["qwen-image-2.1"]["family"] == "qwenimage21" and items["qwen-image-2.1"]["supported"]
    assert items["video:some-ltx"]["family"] == "ltx2"
    assert items["video:some-ltx"]["capabilities"]["audio"] is True
    fn = items["qwen3.8-flash-next@UD-IQ4_XS"]
    assert fn["meta"]["mtp"].endswith("mtp-Qwen3.8-Flash-Next-Q8_0.gguf")
    assert fn["meta"]["mmproj"] is None, "a projector outside the repo folder is not attached"
    assert fn["llama_args"] == ["-lzm", "on"]
    assert caps_mod.model_kinds(fn) == ["chat", "code", "reasoning"]
    assert not any("00002-of" in i for i in items), "later shards are not listed as models"


def test_registry_tells_fine_tunes_with_the_base_file_names_apart(tmp_path):
    from sarness_engine.models.registry import ModelRegistry

    cfg = load_config(tmp_path / "data")
    cfg.ensure_dirs()
    llm = cfg.models_dir / "llm"
    shard = "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"
    _gguf(llm / "unsloth--Qwen3.8-Flash-Next-GGUF" / "UD-IQ4_XS" / shard)
    _gguf(llm / "huihui-ai--Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF" / "unsloth-UD-IQ4_XS" / shard)
    # mradermacher names projectors "<model>.mmproj-f16.gguf"; unsloth keeps the MTP head in MTP/.
    mr = llm / "mradermacher--Huihui-Qwen3.5-27B-abliterated-GGUF"
    _gguf(mr / "Huihui-Qwen3.5-27B-abliterated.Q6_K.gguf")
    (mr / "Huihui-Qwen3.5-27B-abliterated.mmproj-f16.gguf").write_bytes(b"GGUF")
    q = llm / "unsloth--Qwen3.8-27B-GGUF"
    _gguf(q / "Qwen3.8-27B-UD-Q6_K.gguf")
    (q / "mmproj-F16.gguf").write_bytes(b"GGUF")
    (q / "MTP").mkdir()
    (q / "MTP" / "mtp-Qwen3.8-27B-Q4_0.gguf").write_bytes(b"GGUF")
    img = cfg.models_dir / "image" / "Tongyi-MAI--Z-Image-Turbo"
    img.mkdir(parents=True)
    (img / "model_index.json").write_text('{"_class_name": "ZImagePipeline"}')
    items = {i["id"]: i for i in ModelRegistry(Database(cfg.db_path), cfg.models_dir).scan()}
    assert "qwen3.8-flash-next@UD-IQ4_XS" in items
    abl = items["huihui-qwen3.8-flash-next-abliterated@UD-IQ4_XS"]
    assert "huihui" in abl["path"] and abl["llama_args"] == ["-lzm", "on"]
    q35 = items["huihui-qwen3.5-27b-abliterated@Q6_K"]
    assert q35["meta"]["mmproj"].endswith(".mmproj-f16.gguf") and q35["capabilities"]["vision"]
    assert not any("mmproj" in i for i in items), "projectors are never listed as models"
    q38 = items["qwen3.8-27b@UD-Q6_K"]
    assert q38["meta"]["mtp"].endswith("MTP/mtp-Qwen3.8-27B-Q4_0.gguf")
    z = items["z-image-turbo"]
    assert z["family"] == "zimage" and z["supported"] and z["capabilities"]["negative_prompt"] is False


class _Ctx:
    def __init__(self):
        self.notes, self.steps = {}, []

    def annotate(self, **kw):
        self.notes.update(kw)

    def progress(self, step, total, text=None, preview=None):
        self.steps.append((step, total))


class _FakeQwenPipe:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        from PIL import Image

        self.calls.append(kw)
        kw["callback_on_step_end"](self, 0, 0, {})
        return SimpleNamespace(images=[Image.new("RGB", (kw["width"], kw["height"]))] * kw["num_images_per_prompt"])


def test_native_image_family_runs_end_to_end():
    pytest.importorskip("torch")
    from sarness_engine.runtime.models import ModelHandle
    from sarness_engine.workflow.nodes import diffusion as d
    from sarness_engine.workflow.types import WorkflowError

    pipe = _FakeQwenPipe()
    h = ModelHandle("image:q", "qwen-image-2.1", "qwenimage21", {"name": "Qwen-Image 2.1", "limits": {"multiple": 16}},
                    pipe, "gpu")
    ctx = _Ctx()
    cond = d.encode_prompt(ctx, {"model": h}, {"prompt": "a sign that says SARNESS", "negative_prompt": "",
                                               "clip_skip": 0})["conditioning"]
    lat = {"samples": None, "width": 1000, "height": 760, "batch": 2}
    params = {"seed": 7, "steps": 20, "guidance": 4.0, "denoise": 1.0, "guidance_rescale": 0.0}
    out = d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": lat}, params)["latent"]
    call = pipe.calls[0]
    assert (call["width"], call["height"]) == (992, 752), "snapped to multiples of 16"
    assert call["true_cfg_scale"] == 4.0 and call["negative_prompt"] == " "
    imgs = d.vae_decode(ctx, {"model": h, "latent": out}, {"tiled": False})["image"]
    assert len(imgs) == 2 and imgs[0].size == (992, 752)
    with pytest.raises(WorkflowError, match="does not support image-to-image"):
        d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": {**lat, "source": [object()]}}, params)
    params["guidance"] = 1.0
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": lat}, params)
    assert "true_cfg_scale" not in pipe.calls[-1]


def test_ltx2_sampler_returns_frames_and_audio(monkeypatch):
    pytest.importorskip("torch")
    import numpy as np
    from PIL import Image

    from sarness_engine.runtime.models import ModelHandle
    from sarness_engine.workflow.nodes import video as v

    class Pipe:
        vocoder = SimpleNamespace(config={"output_sampling_rate": 24000})

        def __call__(self, **kw):
            self.kw = kw
            kw["callback_on_step_end"](self, 0, 0, {})
            frames = np.zeros((1, kw["num_frames"], kw["height"], kw["width"], 3), dtype=np.float32)
            return frames, [np.zeros((2, 4800), dtype=np.float32)]

    pipe = Pipe()
    h = ModelHandle("video:l", "ltx-2.3", "ltx2", {"name": "LTX", "capabilities": {"modes": ["t2v", "i2v"]},
                                                     "limits": {"multiple": 32, "frame_step": 8}}, pipe, "gpu")
    ctx = _Ctx()
    cond = v.encode_video_prompt(ctx, {"model": h}, {"prompt": "waves", "negative_prompt": "blur"})["conditioning"]
    params = {"width": 770, "height": 515, "frames": 30, "fps": 24, "seed": 1, "steps": 4, "guidance": 3.0,
              "flow_shift": 3.0}
    out = v.video_sampler(ctx, {"model": h, "conditioning": cond}, params)["video"]
    assert (pipe.kw["width"], pipe.kw["height"], pipe.kw["num_frames"]) == (768, 512, 25)
    assert len(out["frames"]) == 25 and isinstance(out["frames"][0], Image.Image)
    assert out["audio"]["rate"] == 24000 and out["audio"]["samples"].shape == (2, 4800)
    assert v.trim(ctx, {"video": out}, {"start_frame": 0, "end_frame": 5})["video"]["audio"] is None


def test_audio_is_muxed_into_mp4(tmp_path):
    import subprocess

    pytest.importorskip("imageio_ffmpeg")

    import imageio.v2 as iio
    import imageio_ffmpeg
    import numpy as np

    from sarness_engine.workflow.nodes.output import _mux_audio

    path = tmp_path / "clip.mp4"
    w = iio.get_writer(str(path), format="FFMPEG", fps=24, codec="libx264", macro_block_size=1,
                       ffmpeg_params=["-pix_fmt", "yuv420p"])
    for _ in range(24):
        w.append_data(np.zeros((64, 64, 3), dtype=np.uint8))
    w.close()
    t = np.linspace(0, 1, 24000, dtype=np.float32)
    assert _mux_audio(path, {"samples": np.stack([np.sin(440 * 2 * np.pi * t)] * 2), "rate": 24000}, "mp4")
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True).stderr
    assert "Audio: aac" in probe and "Video: h264" in probe
    assert not list(tmp_path.glob("*.wav")) and not list(tmp_path.glob("*.muxed.*"))


def test_a_soundtrack_never_cuts_frames_off_the_clip(tmp_path):
    pytest.importorskip("imageio_ffmpeg")

    import imageio.v2 as iio
    import imageio_ffmpeg
    import numpy as np

    from sarness_engine.workflow.nodes.output import _mux_audio

    # LTX-2 returns 121 frames at 24 fps (5.04 s) with a 5.01 s soundtrack; ffmpeg's -shortest dropped 4 frames.
    for secs in (5.01, 5.3):
        path = tmp_path / f"clip{secs}.mp4"
        w = iio.get_writer(str(path), format="FFMPEG", fps=24, codec="libx264", macro_block_size=1,
                           ffmpeg_params=["-pix_fmt", "yuv420p"])
        for i in range(121):
            w.append_data(np.full((64, 64, 3), i * 2, dtype=np.uint8))
        w.close()
        audio = np.zeros((2, int(secs * 24000)), dtype=np.float32)
        assert _mux_audio(path, {"samples": audio, "rate": 24000}, "mp4", 121 / 24)
        assert imageio_ffmpeg.count_frames_and_secs(str(path))[0] == 121, secs


def test_downloads_resume_after_restart_oldest_first(tmp_path):
    from sarness_engine.models.downloads import DownloadManager

    db = Database(tmp_path / "d.db")
    for i, status in enumerate(["downloading", "queued", "paused", "queued", "completed"]):
        db.insert("downloads", {"id": f"d{i}", "model_id": f"m{i}", "name": f"m{i}", "category": "image", "repo": "r",
                                "revision": "main", "files": [], "dest": str(tmp_path / f"m{i}"), "total_bytes": 1,
                                "done_bytes": 0, "status": status, "meta": {}, "created_at": i, "updated_at": i})
    started = []
    dm = DownloadManager(db, lambda *a: None, lambda: "", lambda r: None, max_parallel=2)
    dm._run = lambda did, stop: started.append(did)
    dm.recover()
    for t in list(dm._threads.values()):
        t.join(5)
    assert started == ["d0", "d1"], "interrupted and waiting downloads continue in their original order"
    status = {r["id"]: r["status"] for r in db.all("SELECT id, status FROM downloads")}
    assert status["d2"] == "paused" and status["d3"] == "queued" and status["d4"] == "completed"

    db.execute("UPDATE downloads SET status = 'downloading' WHERE id IN ('d0', 'd1')")
    DownloadManager(db, lambda *a: None, lambda: "", lambda r: None).recover(resume=False)
    status = {r["id"]: r["status"] for r in db.all("SELECT id, status FROM downloads")}
    assert [status[k] for k in ("d0", "d1", "d3")] == ["paused"] * 3


def test_download_retries_dropped_connections_from_where_the_file_stopped(tmp_path, monkeypatch):
    import httpx

    from sarness_engine.models import downloads as dl

    body = bytes(range(256)) * 40
    seen = []

    class Drops(httpx.SyncByteStream):
        def __iter__(self):
            yield body[:1000]
            raise httpx.ReadError("[Errno 104] Connection reset by peer")

    def handler(request):
        rng = request.headers.get("range")
        seen.append(rng)
        if len(seen) == 1:
            return httpx.Response(200, stream=Drops())
        if len(seen) == 2:
            return httpx.Response(503)
        start = int(rng.split("=")[1].rstrip("-"))
        return httpx.Response(206, content=body[start:])

    monkeypatch.setattr(dl, "_client", lambda token: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(dl, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(dl, "CHUNK", 100)  # httpx hands out whole chunks; keep the 1000 bytes before the drop
    db = Database(tmp_path / "d.db")
    dest = tmp_path / "m"
    db.insert("downloads", {"id": "d0", "model_id": "m", "name": "m", "category": "image", "repo": "r",
                            "revision": "main", "files": [{"path": "w.safetensors", "size": len(body)}],
                            "dest": str(dest), "total_bytes": len(body), "done_bytes": 0, "status": "downloading",
                            "meta": {}, "created_at": 0, "updated_at": 0})
    dm = dl.DownloadManager(db, lambda *a: None, lambda: "", lambda r: None)
    dm._pump = lambda: None
    dm._run("d0", threading.Event())
    assert seen == [None, "bytes=1000-", "bytes=1000-"], "each retry continues from the partial file"
    assert dm.get("d0")["status"] == "completed" and (dest / "w.safetensors").read_bytes() == body

    seen.clear()
    (dest / "w.safetensors").unlink()
    db.execute("UPDATE downloads SET status = 'downloading' WHERE id = 'd0'")
    monkeypatch.setattr(dl, "_client", lambda token: httpx.Client(transport=httpx.MockTransport(
        lambda request: seen.append(1) or httpx.Response(503))))
    dm._run("d0", threading.Event())
    row = dm.get("d0")
    assert len(seen) == 3 and row["status"] == "failed" and "gave up after 3 attempts" in row["error"]


def test_unified_memory_loads_every_component_without_mmap(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")

    from sarness_engine.runtime import models as rm

    (tmp_path / "text_encoder").mkdir()
    (tmp_path / "model_index.json").write_text(
        '{"_class_name": "X", "text_encoder": ["transformers", "CLIPTextModel"], "tokenizer": ["transformers", "CLIPTokenizer"],'
        ' "unet": ["diffusers", "UNet2DConditionModel"], "safety_checker": [null, null]}')
    calls = []
    monkeypatch.setattr(transformers.CLIPTextModel, "from_pretrained",
                        classmethod(lambda c, p, **kw: calls.append(("te", kw)) or "TE"))

    class Pipe:
        @classmethod
        def from_pretrained(cls, p, **kw):
            calls.append(("pipe", kw))
            return "PIPE"

    monkeypatch.setattr(rm, "unified_memory", lambda: True)
    assert rm.load_pipeline(Pipe, tmp_path, "bf16", variant="fp16") == "PIPE"
    assert calls[0] == ("te", {"subfolder": "text_encoder", "dtype": "bf16", "variant": None, "disable_mmap": True})
    assert calls[1][1] == {"dtype": "bf16", "disable_mmap": True, "variant": "fp16", "text_encoder": "TE"}

    calls.clear()
    monkeypatch.setattr(rm, "unified_memory", lambda: False)
    rm.load_pipeline(Pipe, tmp_path, "bf16")
    assert calls == [("pipe", {"dtype": "bf16"})], "discrete GPUs load exactly as before"
    assert rm.fast_load() == {}


def test_llama_reads_model_up_front_on_unified_memory(tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(args, **kw):
        seen["args"] = args
        raise Stop

    helps = {"/bin/new": "-lm,   --load-mode MODE   model loading mode\n - dio: use DirectIO if available",
             "/bin/mid": "-lm,   --load-mode MODE   model loading mode", "/bin/old": "--mmap, --no-mmap", "/bin/x": ""}
    monkeypatch.setattr(llama.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout=helps[args[0]]))
    monkeypatch.setattr(llama.subprocess, "Popen", fake_popen)
    llama.no_mmap_args.cache_clear()
    srv = llama.LlamaServer.__new__(llama.LlamaServer)
    for binary, unified, expect in [("/bin/new", True, ["--load-mode", "dio"]), ("/bin/mid", True, ["--load-mode", "none"]),
                                    ("/bin/old", True, ["--no-mmap"]), ("/bin/x", True, []), ("/bin/new", False, [])]:
        monkeypatch.setattr(llama, "gpus", lambda: [{"unified_memory": unified}])
        srv.__init__(SimpleNamespace(binary=lambda: binary, version=lambda: "x", env=lambda: {}), tmp_path)
        with pytest.raises(Stop):
            srv.load({"id": "m", "path": str(tmp_path / "m.gguf")}, ctx=4096, gpu_layers=999, flash_attn=True)
        args = seen["args"]
        assert [a for a in args if a in ("--load-mode", "dio", "none", "--no-mmap")] == expect, binary
    llama.no_mmap_args.cache_clear()


def test_stopping_the_gpu_worker_survives_the_reader_seeing_it_exit():
    """llm/load stopped the idle diffusion worker; the reader thread cleared the process as soon as the
    worker exited, while stop() was still waiting for it (AttributeError: 'NoneType' ... 'is_alive')."""
    from sarness_engine.jobs.supervisor import WorkerSupervisor

    sup = WorkerSupervisor({}, lambda m: None, lambda job, reason: None)

    class Proc:
        exited = False

        def join(self, timeout=None):
            self.exited = True
            sup._proc = None  # what the reader thread does when the pipe closes

        def is_alive(self):
            return not self.exited

        def kill(self):
            raise AssertionError("an exited worker is not killed")

    sup._proc, sup._conn = Proc(), SimpleNamespace(send=lambda msg: None)
    sup.stop()
    assert sup._proc is None and not sup.alive


def test_any_loaded_model_can_be_unloaded_from_the_models_list(client):
    """Image/video models held by the GPU worker get the same Unload as the chat model."""
    st = client.app.state.sarness
    mid = next(m["id"] for m in client.get("/v1/models").json() if m["category"] == "image")
    released = []

    class Sup:
        busy = True
        last_status = {"loaded": [{"key": f"image:{mid}", "vram_mb": 9000}, {"key": "video:other", "vram_mb": 1}]}

        def release(self, key=None, wait=0.0):
            released.append((key, wait))
            self.last_status = {"loaded": [m for m in self.last_status["loaded"] if m["key"] != key]}

        def stop(self):
            pass

    st.jobs.supervisor = sup = Sup()
    assert next(m for m in client.get("/v1/models").json() if m["id"] == mid)["loaded"]
    r = client.post(f"/v1/models/{mid}/unload")
    assert r.status_code == 409 and "job is running" in r.json()["detail"] and not released
    sup.busy = False
    assert client.post(f"/v1/models/{mid}/unload").json() == {"ok": True}
    assert released == [(f"image:{mid}", 60.0)], "only that model is released, and the call waits for it"
    assert not next(m for m in client.get("/v1/models").json() if m["id"] == mid)["loaded"]
    assert sup.last_status["loaded"] == [{"key": "video:other", "vram_mb": 1}]
    assert client.post("/v1/models/not-loaded/unload").json() == {"ok": True}, "unloading is idempotent"


def test_a_job_whose_worker_was_killed_for_memory_fails_instead_of_retrying(client):
    jobs = client.app.state.sarness.jobs
    ids = [jobs.submit(kind="image", simple={"model": "image:my-sdxl", "prompt": p})["id"]
           for p in ("a", "b")]
    jobs._on_worker_crash(ids[0], "GPU worker exited unexpectedly (exit code 1)", True)
    jobs._on_worker_crash(ids[1], "The GPU worker was stopped by the system for running out of memory.", False)
    first, second = (client.get(f"/v1/jobs/{i}").json() for i in ids)
    assert first["status"] == "queued" and first["attempts"] == 1, "an ordinary crash is retried"
    assert second["status"] == "failed" and "out of memory" in second["error"]["message"], \
        "running the same job again would only exhaust memory again"


def test_an_out_of_memory_job_is_retried_with_offload_only_where_offloading_saves_memory(client, monkeypatch):
    from sarness_engine import hardware

    jobs = client.app.state.sarness.jobs
    for unified, status in ((False, "queued"), (True, "failed")):
        monkeypatch.setattr(hardware, "gpus", lambda: [{"name": "GPU", "unified_memory": unified, "vram_total_mb": 24576,
                                                         "vram_free_mb": 24576, "vram_used_mb": 0}])
        job_id = jobs.submit(kind="image", simple={"model": "image:my-sdxl", "prompt": "a"})["id"]
        jobs._handle_failure(job_id, {"type": "failed", "message": "The GPU ran out of memory.", "oom": True})
        assert client.get(f"/v1/jobs/{job_id}").json()["status"] == status, f"unified={unified}"


def test_the_supervisor_stops_the_worker_before_the_machine_runs_out_of_memory(monkeypatch):
    import psutil

    from sarness_engine.jobs import supervisor as sv

    avail = iter([20_000, 9_000, 3_000])
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=128 * 2**30,
                                                                          available=next(avail) * 2**20))

    class Proc:
        killed = False

        def is_alive(self):
            return not self.killed

        def kill(self):
            self.killed = True

    proc = Proc()
    sup = sv.WorkerSupervisor({}, lambda m: None, lambda *a: None)
    sup._guard_memory(proc, interval=0)
    assert proc.killed, "3 GB left is below the 3 % floor (3.8 GB of 128 GB)"
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=32 * 2**30, available=0))
    assert sv.memory_floor_mb() == 4096
