"""Experiments framework and God's Paintbrush (CPU only: the GPU handler is replaced by a fake)."""

import asyncio
import base64
import io
import os
import threading
import time
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sarness_engine.app import create_app
from sarness_engine.config import load_config, save_config
from sarness_engine.db import new_id, now
from sarness_engine.experiments.paintbrush import components as C
from sarness_engine.experiments.paintbrush import masks as MK
from sarness_engine.experiments.paintbrush import materials as M
from sarness_engine.experiments.paintbrush.service import MAX_REFINE_PIXELS, refine_size, PaintbrushError
from sarness_engine.experiments.registry import Experiment, ExperimentRegistry
from sarness_engine.experiments.scheduler import LatestWinsScheduler, RequestCancelled, Superseded
from sarness_engine.experiments.worker import ExperimentWorker, WorkerError


def scene(w=768, h=512):
    """Sky over mountains over grass, with a lake on the right."""
    labels = np.full((h, w), M.INDEX["sky"], np.uint8)
    labels[h // 3:] = M.INDEX["mountains"]
    labels[h // 2:] = M.INDEX["grass"]
    labels[2 * h // 3:, w // 2:] = M.INDEX["water"]
    return labels


def rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ------------------------------------------------------------------ materials
def test_materials_map_to_real_model_classes():
    ids = [m.id for m in M.MATERIALS]
    assert len(ids) == len(set(ids)) and M.MAX_INDEX == len(M.MATERIALS)
    assert len({m.color.lower() for m in M.MATERIALS}) == len(M.MATERIALS), "interface colours must be distinct"
    for i, m in enumerate(M.MATERIALS, start=1):
        assert M.INDEX[m.id] == i and M.by_index(i) is m
        assert m.coco_id in M.COCO_NAMES, m.id
        assert m.ade_index in M.ADE_COLORS and m.ade_index in M.ADE_NAMES, m.id
        assert m.live in ("native", "experimental") and m.refine in ("native", "approximate")
        if m.ade_index == 0 or m.refine == "approximate" or m.live == "experimental":
            assert m.note, f"{m.id} is not fully supported and must say why"
    # Canvas-equivalent palette the spec asks for
    for required in ("sky", "clouds", "water", "ocean", "river", "grass", "trees", "forest", "mountains", "rocks",
                     "snow", "sand", "dirt", "flowers", "bushes", "buildings"):
        assert required in M.INDEX
    # the model protocols, not the interface colours, decide what the networks see
    assert M.COCO_NAMES[M.MATERIALS[M.INDEX["sky"] - 1].coco_id] == "sky-other"
    assert M.COCO_NAMES[M.MATERIALS[M.INDEX["ocean"] - 1].coco_id] == "sea"
    assert M.ADE_COLORS[M.MATERIALS[M.INDEX["grass"] - 1].ade_index] == (4, 250, 7)
    assert M.ADE_COLORS[M.MATERIALS[M.INDEX["sky"] - 1].ade_index] == (6, 230, 230)
    assert M.ADE_COLORS[M.MATERIALS[M.INDEX["mountains"] - 1].ade_index] == (143, 255, 140)


def test_public_materials_carry_support_flags():
    pub = M.public_list()
    assert [p["index"] for p in pub] == list(range(1, M.MAX_INDEX + 1))
    snow = next(p for p in pub if p["id"] == "snow")
    assert snow["refine"] == "approximate" and snow["note"]
    assert {"id", "label", "color", "group", "live", "refine", "note"} <= set(snow)


# ---------------------------------------------------------------------- masks
@pytest.mark.parametrize("encoding", ["deflate", "raw"])
def test_mask_round_trip(encoding):
    labels = scene()
    out = MK.decode(MK.encode(labels, encoding))
    assert out.dtype == np.uint8 and out.shape == (512, 768)
    assert np.array_equal(out, labels)


def test_mask_accepts_browser_deflate_stream():
    labels = scene(512, 256)
    co = zlib.compressobj(9, zlib.DEFLATED, 15)  # CompressionStream('deflate') = zlib wrapper
    data = co.compress(labels.tobytes()) + co.flush()
    out = MK.decode({"width": 512, "height": 256, "encoding": "deflate", "data": base64.b64encode(data).decode()})
    assert np.array_equal(out, labels)


@pytest.mark.parametrize("mask, message", [
    (None, "must be an object"),
    ({"height": 512}, "width and height"),
    ({"width": 700, "height": 512, "data": ""}, "multiple of 32"),
    ({"width": 128, "height": 512, "data": ""}, "between"),
    ({"width": 2048, "height": 512, "data": ""}, "between"),
    ({"width": 512, "height": 512, "encoding": "raw", "data": "not base64!"}, "base64"),
    ({"width": 512, "height": 512, "encoding": "deflate", "data": base64.b64encode(b"nope").decode()}, "deflate"),
    ({"width": 512, "height": 512, "encoding": "gzip", "data": ""}, "encoding"),
    ({"width": 512, "height": 512, "encoding": "raw", "data": base64.b64encode(b"\x01" * 100).decode()}, "bytes"),
])
def test_mask_validation(mask, message):
    with pytest.raises(MK.MaskError, match=message):
        MK.decode(mask)


def test_mask_rejects_unknown_material_indices():
    labels = scene(256, 256)
    labels[0, 0] = 0
    with pytest.raises(MK.MaskError, match="index 0"):
        MK.decode(MK.encode(labels))
    labels[0, 0] = M.MAX_INDEX + 1
    with pytest.raises(MK.MaskError, match=f"index {M.MAX_INDEX + 1}"):
        MK.decode(MK.encode(labels))


def test_mask_decompression_is_bounded():
    bomb = zlib.compress(b"\x01" * (64 * 1024 * 1024), 9)  # 64 MiB of label bytes for a 256×256 canvas
    assert len(bomb) < 200_000
    with pytest.raises(MK.MaskError, match="expected 256×256"):
        MK.decode({"width": 256, "height": 256, "encoding": "deflate", "data": base64.b64encode(bomb).decode()})


def test_label_conversions_are_exact_per_region():
    labels = scene()
    coco = MK.to_coco(labels)
    ade = MK.to_ade_rgb(labels)
    color = MK.to_color(labels)
    for mid, (y, x) in {"sky": (10, 10), "mountains": (200, 100), "grass": (300, 100), "water": (500, 700)}.items():
        m = M.MATERIALS[M.INDEX[mid] - 1]
        assert coco[y, x] == m.coco_id
        assert tuple(ade[y, x]) == M.ADE_COLORS[m.ade_index]
        assert tuple(color[y, x]) == rgb(m.color)
    assert coco.dtype == np.uint8 and ade.shape == (512, 768, 3)


def test_coverage_and_prompt():
    labels = scene()
    cov = MK.coverage(labels)
    assert set(cov) == {"sky", "mountains", "grass", "water"}
    assert abs(sum(cov.values()) - 1) < 1e-3
    p = MK.build_prompt(labels, "  golden hour  ")
    assert p.startswith("golden hour, a photograph of a natural landscape with ")
    order = [M.MATERIALS[M.INDEX[k] - 1].prompt for k in sorted(cov, key=lambda k: -cov[k])]
    positions = [p.index(t) for t in order]
    assert positions == sorted(positions), "materials are listed by painted area"
    tiny = labels.copy()
    tiny[0, :8] = M.INDEX["flowers"]  # far below 1 % of the canvas
    assert M.MATERIALS[M.INDEX["flowers"] - 1].prompt not in MK.build_prompt(tiny)


@pytest.mark.parametrize("fmt", ["coco", "ade", "color", "index"])
def test_export_png_formats(fmt):
    labels = scene(256, 256)
    img = np.array(Image.open(io.BytesIO(MK.export_png(labels, fmt))))
    expected = {"coco": MK.to_coco, "ade": MK.to_ade_rgb, "color": MK.to_color, "index": lambda x: x}[fmt](labels)
    assert np.array_equal(img, expected)


def test_export_png_rejects_unknown_format():
    with pytest.raises(MK.MaskError):
        MK.export_png(scene(256, 256), "jpeg")


def test_refine_sizes():
    assert refine_size(768, 512, 1.0) == (768, 512)
    assert refine_size(768, 512, 1.5) == (1152, 768)
    assert refine_size(768, 512, 2.0) == (1536, 1024)
    assert refine_size(512, 768, 2.0) == (1024, 1536)
    with pytest.raises(PaintbrushError, match="scale"):
        refine_size(768, 512, 3.0)
    with pytest.raises(PaintbrushError, match="verified refine limit"):
        refine_size(1024, 1024, 2.0)
    w, h = refine_size(896, 512, 1.5)
    assert w % 64 == 0 and h % 64 == 0 and w * h <= MAX_REFINE_PIXELS


# ------------------------------------------------------------------ scheduler
def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.events: dict[str, asyncio.Event] = {}

    async def execute(self, t):
        self.started.append(t.payload)
        ev = self.events.setdefault(t.id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), self.delay)
            raise RuntimeError("cancelled by the worker")
        except asyncio.TimeoutError:
            return t.payload

    def cancel_running(self, t):
        self.cancelled.append(t.payload)
        self.events.setdefault(t.id, asyncio.Event()).set()


def test_scheduler_newest_request_replaces_queued_one():
    async def go():
        r = Recorder()
        s = LatestWinsScheduler(r.execute, r.cancel_running)
        s.start()
        a = s.submit("canvas", "live", "a")
        await asyncio.sleep(0.01)          # a is running
        b = s.submit("canvas", "live", "b")
        c = s.submit("canvas", "live", "c")
        results = await asyncio.gather(a.future, b.future, c.future, return_exceptions=True)
        await s.stop()
        return r, results

    r, (a, b, c) = run(go())
    assert a == "a", "fast kinds finish so continuous painting keeps producing frames"
    assert isinstance(b, Superseded)
    assert c == "c"
    assert r.started == ["a", "c"] and r.cancelled == []


def test_scheduler_preempts_a_running_slow_request():
    async def go():
        r = Recorder(delay=1.0)
        s = LatestWinsScheduler(r.execute, r.cancel_running, preemptible={"refine"})
        s.start()
        a = s.submit("canvas", "refine", "a")
        await asyncio.sleep(0.01)
        t0 = time.perf_counter()
        b = s.submit("canvas", "refine", "b")
        ra = await asyncio.gather(a.future, return_exceptions=True)
        waited = time.perf_counter() - t0
        r.delay = 0.01
        rb = await b.future
        await s.stop()
        return r, ra[0], rb, waited

    r, a, b, waited = run(go())
    assert isinstance(a, Superseded) and b == "b"
    assert r.cancelled == ["a"] and waited < 0.5


def test_scheduler_sessions_are_independent_and_fifo():
    async def go():
        r = Recorder(delay=0.02)
        s = LatestWinsScheduler(r.execute, r.cancel_running, preemptible={"refine"})
        s.start()
        a = s.submit("one", "refine", "one-1")
        await asyncio.sleep(0.005)
        b = s.submit("two", "refine", "two-1")
        c = s.submit("three", "live", "three-1")
        out = await asyncio.gather(a.future, b.future, c.future)
        await s.stop()
        return r, out

    r, out = run(go())
    assert out == ["one-1", "two-1", "three-1"]
    assert r.started == ["one-1", "two-1", "three-1"] and r.cancelled == []


def test_scheduler_cancel_session_and_cancel_all():
    async def go():
        r = Recorder(delay=1.0)
        s = LatestWinsScheduler(r.execute, r.cancel_running)
        s.start()
        a = s.submit("one", "live", "a")
        await asyncio.sleep(0.01)
        b = s.submit("two", "live", "b")
        n = s.cancel("one")
        ra = await asyncio.gather(a.future, return_exceptions=True)
        c = s.submit("three", "live", "c")
        s.cancel_all()
        rest = await asyncio.gather(b.future, c.future, return_exceptions=True)
        await s.stop()
        return n, ra[0], rest, s

    n, a, (b, c), s = run(go())
    assert n == 1 and isinstance(a, RequestCancelled)
    assert isinstance(b, RequestCancelled) and isinstance(c, RequestCancelled)
    assert s.queued == 0 and s.running is None


# ---------------------------------------------------------------- registry
class Dummy(Experiment):
    id = "dummy"
    name = "Dummy"

    def __init__(self, active=False, broken=False):
        self.active, self.broken, self.yielded = active, broken, []

    def gpu_active(self):
        return self.active

    async def yield_gpu(self, reason):
        if self.broken:
            raise RuntimeError("no")
        self.yielded.append(reason)
        self.active = False

    async def startup(self):
        if self.broken:
            raise RuntimeError("cannot start")


def test_registry_yields_only_active_experiments_and_survives_failures():
    reg = ExperimentRegistry()
    a, b = Dummy(active=True), Dummy(broken=True, active=True)
    b.id = "broken"
    reg.register(a)
    reg.register(b)
    with pytest.raises(ValueError):
        reg.register(Dummy())
    run(reg.startup())  # the broken experiment does not stop the others
    assert reg.gpu_active()
    run(reg.yield_gpu("job j_1"))
    assert a.yielded == ["job j_1"] and not a.gpu_active()
    assert [e.id for e in reg.list()] == ["dummy", "broken"]


# ------------------------------------------------------------- worker process
def _events():
    seen = []
    return seen, seen.append


def test_experiment_worker_runs_requests_in_a_child_process(tmp_path):
    seen, on_event = _events()
    w = ExperimentWorker("fake", "fake_experiment_handler:Handler", {"scale": 3}, tmp_path, on_event)
    try:
        out = w.submit("double", {"x": 7}).result(30)
        assert out["y"] == 21 and out["pid"] != os.getpid()
        assert w.alive and w.info["pid"] == out["pid"]
        assert w.submit("slow", {"steps": 3}).result(10) == {"done": True}
        assert [e["step"] for e in seen if e["type"] == "progress"] == [1, 2, 3]
        with pytest.raises(WorkerError) as err:
            w.submit("fail", {}).result(10)
        assert err.value.code == "boom" and "broke" in str(err.value)
        fut = w.submit("slow", {"steps": 500}, req_id="long")
        time.sleep(0.1)
        w.cancel("long")
        with pytest.raises(WorkerError) as err:
            fut.result(10)
        assert err.value.code == "cancelled"
        w.release("refine")
        deadline = time.time() + 5
        while w.query_status().get("released") != ["refine"] and time.time() < deadline:
            time.sleep(0.05)
        assert w.last_status["released"] == ["refine"]
        assert w.submit("double", {"x": 1}).result(10)["y"] == 3, "the worker survives handler errors"
    finally:
        w.stop()
    assert not w.alive and seen[-1]["type"] == "stopped"


def test_experiment_worker_crash_is_contained_and_restarts(tmp_path):
    seen, on_event = _events()
    w = ExperimentWorker("fake", "fake_experiment_handler:Handler", {"scale": 2}, tmp_path, on_event)
    try:
        first = w.submit("double", {"x": 1}).result(30)["pid"]
        with pytest.raises(WorkerError) as err:
            w.submit("crash", {}).result(10)
        assert err.value.code == "crashed"
        deadline = time.time() + 5
        while w.alive and time.time() < deadline:
            time.sleep(0.05)
        assert any(e["type"] == "crashed" and e["exit_code"] == 3 for e in seen)
        second = w.submit("double", {"x": 2}).result(30)
        assert second["y"] == 4 and second["pid"] != first
    finally:
        w.stop()


# ------------------------------------------------------------------------ API
@pytest.fixture
def client(tmp_path):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    save_config(cfg)
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        c.st = app.state.sarness
        yield c


@pytest.fixture
def painter(client, monkeypatch):
    """The paintbrush with 'installed' models and the GPU handler swapped for a CPU fake."""
    exp = client.st.experiments.get("gods-paintbrush")
    exp.worker.stop()
    exp.worker = ExperimentWorker("gods-paintbrush", "fake_experiment_handler:FakePaintbrush", {},
                                  client.st.cfg.logs_dir, exp._on_worker_event)
    monkeypatch.setattr(exp, "availability", lambda: {"state": "ready", "live": True, "refine": True, "reasons": []})
    yield exp
    exp.worker.stop()


def test_experiment_catalogue(client):
    items = client.get("/v1/experiments").json()
    pb = next(i for i in items if i["id"] == "gods-paintbrush")
    assert pb["name"] == "God's Paintbrush"
    assert pb["tagline"] == "Turn simple painted shapes into photorealistic landscapes using AI."
    assert pb["availability"]["state"] in ("needs_install", "unavailable") and pb["gpu_active"] is False
    assert client.get("/v1/experiments/nope").status_code == 404
    sysinfo = client.get("/v1/system").json()
    assert {"id": "gods-paintbrush", "gpu_active": False} in sysinfo["runtime"]["experiments"]


def test_paintbrush_status_without_models(client):
    s = client.get("/v1/experiments/gods-paintbrush/status").json()
    assert [m["id"] for m in s["materials"]] == [m.id for m in M.MATERIALS]
    assert {c["id"] for c in s["components"]} == {"spade-landscapes-512", "sd15", "controlnet-seg"}
    assert not any(c["installed"] for c in s["components"])
    assert all(c["license"] for c in s["components"])
    assert s["runtime"]["worker"] is False and s["install"]["running"] is False
    assert s["canvas"]["multiple"] == 32 and s["refine"]["scales"] == [1.0, 1.5, 2.0]
    r = client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())})
    assert r.status_code == 409 and "GauGAN" in r.json()["detail"]
    assert client.post("/v1/experiments/gods-paintbrush/warmup", json={}).status_code == 409


def test_installed_detection(tmp_path):
    c = next(x for x in C.COMPONENTS if x.id == "spade-landscapes-512")
    assert not C.installed(tmp_path, c)
    d = C.component_dir(tmp_path, c.id)
    d.mkdir(parents=True)
    for f in c.required_files:
        (d / f).write_bytes(b"x")
    assert C.installed(tmp_path, c)
    assert C.worker_paths(tmp_path)[c.id] == str(d)


def test_documents_crud(client):
    base = "/v1/experiments/gods-paintbrush/documents"
    data = {"format": "sarness.paintbrush", "version": 1, "layers": [{"name": "Base"}]}
    thumb = "data:image/png;base64," + base64.b64encode(b"\x89PNG....").decode()
    d = client.post(base, json={"title": "Alps", "data": data, "thumbnail": thumb}).json()
    assert d["id"].startswith("xd_") and d["data"] == data and d["experiment_id"] == "gods-paintbrush"
    listing = client.get(base).json()
    assert [x["title"] for x in listing] == ["Alps"] and "data" not in listing[0]
    time.sleep(0.01)
    u = client.put(f"{base}/{d['id']}", json={"title": "Alps at dusk", "data": {**data, "seed": 7}}).json()
    assert u["title"] == "Alps at dusk" and u["data"]["seed"] == 7 and u["updated_at"] >= d["updated_at"]
    dup = client.post(f"{base}/{d['id']}/duplicate", json={}).json()
    assert dup["id"] != d["id"] and dup["title"] == "Alps at dusk (copy)" and dup["data"] == u["data"]
    assert client.get(f"{base}/{d['id']}").json()["thumbnail"] == thumb
    assert client.delete(f"{base}/{d['id']}").json() == {"ok": True}
    assert client.get(f"{base}/{d['id']}").status_code == 404
    assert [x["id"] for x in client.get(base).json()] == [dup["id"]]


def test_documents_validation(client):
    base = "/v1/experiments/gods-paintbrush/documents"
    assert client.post(base, json={"title": "x", "data": [1, 2]}).status_code == 400
    assert client.post(base, json={"title": "x", "data": {}, "thumbnail": "http://evil/x.png"}).status_code == 400
    big = {"blob": "a" * (8 * 1024 * 1024 + 10)}
    assert client.post(base, json={"title": "x", "data": big}).status_code == 413
    d = client.post(base, json={"title": "x", "data": {}}).json()
    assert client.put(f"{base}/{d['id']}", json={"title": "   "}).status_code == 400
    assert client.get("/v1/experiments/nope/documents").status_code == 404
    # a document is only reachable through its own experiment
    client.st.db.insert("experiment_documents", {"id": "xd_other", "experiment_id": "other", "title": "t",
                                                 "data": {}, "thumbnail": None, "project_id": None,
                                                 "created_at": now(), "updated_at": now()})
    assert client.get(f"{base}/xd_other").status_code == 404


def test_documents_survive_restart(tmp_path):
    cfg = load_config(tmp_path / "data")
    token = cfg.rotate_token()
    save_config(cfg)
    with TestClient(create_app(cfg, start_background=False)) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        doc = c.post("/v1/experiments/gods-paintbrush/documents", json={"title": "Keep", "data": {"a": 1}}).json()
    with TestClient(create_app(load_config(tmp_path / "data"), start_background=False)) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        assert c.get(f"/v1/experiments/gods-paintbrush/documents/{doc['id']}").json()["data"] == {"a": 1}


def test_mask_export_writes_model_class_values(client):
    r = client.post("/v1/experiments/gods-paintbrush/mask/export", json={"mask": MK.encode(scene()), "format": "coco"})
    assert r.status_code == 200
    body = r.json()
    raw = client.get(body["url"]).content
    img = np.array(Image.open(io.BytesIO(raw)))
    assert np.array_equal(img, MK.to_coco(scene()))
    legend = {x["material"]: x["value"] for x in body["legend"]}
    assert legend == {k: M.MATERIALS[M.INDEX[k] - 1].coco_id for k in ("sky", "mountains", "grass", "water")}
    bad = client.post("/v1/experiments/gods-paintbrush/mask/export", json={"mask": {"width": 3}, "format": "coco"})
    assert bad.status_code == 422


def _image(body):
    head, b64 = body["image"].split(",", 1)
    assert head == f"data:{body['mime']};base64"
    return np.array(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))


def test_render_live_keeps_spatial_correspondence(client, painter):
    labels = scene()
    r = client.post("/v1/experiments/gods-paintbrush/render", json={"session": "s1", "mask": MK.encode(labels)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "live" and (body["width"], body["height"]) == (768, 512)
    assert body["model"] == "spade-landscapes-512" and set(body["coverage"]) == {"sky", "mountains", "grass", "water"}
    assert {"live_ms", "queue_ms", "total_ms"} <= set(body["timings"])
    img = _image(body)
    assert np.array_equal(img, MK.to_color(labels)), "the label map reached the worker pixel-for-pixel"
    edited = labels.copy()
    edited[:, :100] = M.INDEX["trees"]
    img2 = _image(client.post("/v1/experiments/gods-paintbrush/render",
                              json={"session": "s1", "mask": MK.encode(edited)}).json())
    assert not np.array_equal(img, img2) and np.array_equal(img2[:, 100:], img[:, 100:])
    status = client.get("/v1/experiments/gods-paintbrush/status").json()
    assert status["runtime"]["worker"] and status["runtime"]["loaded"]["live"]
    assert status["stats"]["live"]["count"] == 2


def test_render_refine_parameters_and_outputs(client, painter):
    r = client.post("/v1/experiments/gods-paintbrush/render", json={
        "session": "s1", "kind": "refine", "mask": MK.encode(scene()), "seed": 1234, "scale": 1.5,
        "quality": "draft", "creativity": 5, "control": 0.0, "prompt": "misty morning"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (1152, 768) and body["seed"] == 1234
    assert body["creativity"] == 1.0 and body["control"] == 0.3 and body["steps"] == 10
    assert body["prompt"].startswith("misty morning, a photograph of a natural landscape with")
    rid = body["render_id"]
    exp = client.post(f"/v1/experiments/gods-paintbrush/renders/{rid}/export").json()
    assert client.get(exp["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"
    gen = client.post(f"/v1/experiments/gods-paintbrush/renders/{rid}/save", json={"document_id": "xd_1"}).json()
    full = client.get(f"/v1/generations/{gen['id']}").json()
    assert full["model_id"] == "experiment:gods-paintbrush/sd15-controlnet-seg" and full["seed"] == 1234
    meta = client.get(f"/v1/generations/{gen['id']}/metadata").json()
    flat = str(meta)
    assert "gods-paintbrush" in flat and "xd_1" in flat
    assert client.post("/v1/experiments/gods-paintbrush/renders/missing/save", json={}).status_code == 404
    bad = client.post("/v1/experiments/gods-paintbrush/render",
                      json={"kind": "refine", "mask": MK.encode(scene()), "quality": "ultra"})
    assert bad.status_code == 400
    rnd = client.post("/v1/experiments/gods-paintbrush/render",
                      json={"kind": "refine", "mask": MK.encode(scene()), "seed": -1}).json()
    assert 0 <= rnd["seed"] < 2**31


def test_rapid_refines_supersede_older_ones(client, painter):
    mask = MK.encode(scene())
    results: dict[str, object] = {}

    def go(name, delay=0.0):
        time.sleep(delay)
        results[name] = client.post("/v1/experiments/gods-paintbrush/render",
                                    json={"session": "fast", "kind": "refine", "mask": mask})

    # warm the fake worker so both requests race on the scheduler, not on process start-up
    assert client.post("/v1/experiments/gods-paintbrush/render", json={"session": "fast", "mask": mask}).status_code == 200
    threads = [threading.Thread(target=go, args=("old",)), threading.Thread(target=go, args=("new", 0.2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results["old"].status_code == 409 and results["old"].json()["superseded"] is True
    assert results["new"].status_code == 200


def test_cancel_endpoint(client, painter):
    mask = MK.encode(scene())
    client.post("/v1/experiments/gods-paintbrush/render", json={"session": "c", "mask": mask})
    out: dict[str, object] = {}
    t = threading.Thread(target=lambda: out.setdefault("r", client.post(
        "/v1/experiments/gods-paintbrush/render", json={"session": "c", "kind": "refine", "mask": mask})))
    t.start()
    time.sleep(0.2)
    assert client.post("/v1/experiments/gods-paintbrush/cancel", json={"session": "c"}).json()["cancelled"] == 1
    t.join(30)
    assert out["r"].status_code == 409 and out["r"].json()["cancelled"] is True


def test_queued_gpu_job_takes_priority(client, painter):
    client.st.db.insert("jobs", {"id": new_id("j_"), "kind": "image", "status": "queued", "workflow": {},
                                 "created_at": now()})
    r = client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())})
    assert r.status_code == 503 and "waiting for the GPU" in r.json()["detail"]


def test_yield_gpu_stops_the_worker(client, painter):
    assert client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())}).status_code == 200
    assert client.st.experiments.gpu_active()
    client.portal.call(client.st.experiments.yield_gpu, "job j_test")
    assert not painter.worker.alive and not client.st.experiments.gpu_active()
    r = client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())})
    assert r.status_code == 503, "the GPU is held for the job for a few seconds"
    painter._hold_until = 0
    assert client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())}).status_code == 200


def test_release_endpoint(client, painter):
    client.post("/v1/experiments/gods-paintbrush/render", json={"mask": MK.encode(scene())})
    assert painter.worker.alive
    rt = client.post("/v1/experiments/gods-paintbrush/release", json={"level": "all"}).json()
    assert rt["worker"] is False and not painter.worker.alive
    assert client.post("/v1/experiments/gods-paintbrush/release", json={"level": "bogus"}).status_code == 400
