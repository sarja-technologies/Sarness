"""End-to-end GPU test and benchmark for God's Paintbrush against a running Sarness Engine.

Runs real inference on the engine's GPU and asserts on real outputs. Not part of
the default pytest run (needs a GPU, the installed paintbrush models and a
running engine). Run it on the GPU server itself, so nvidia-smi sees the card:

    SARNESS_URL=http://127.0.0.1:8742 SARNESS_TOKEN=... python tests/paintbrush_e2e.py [--out DIR] [--coexist]

``--coexist`` also checks that chat and an image job still work while the
paintbrush holds the GPU (needs a chat endpoint and an image model). Results
(JSON) and every rendered image are written to ``--out``.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw

from sarness_engine.experiments.paintbrush import masks as MK
from sarness_engine.experiments.paintbrush import materials as M

URL = os.environ.get("SARNESS_URL", "http://127.0.0.1:8742")
TOKEN = os.environ.get("SARNESS_TOKEN") or Path(os.environ["SARNESS_TOKEN_FILE"]).read_text().strip()
C = httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=600)
PB = "/v1/experiments/gods-paintbrush"
RESULTS: dict[str, object] = {}
CHECKS: list[dict[str, object]] = []
OUT = Path("paintbrush-e2e-out")


# ------------------------------------------------------------------ helpers
def check(name: str, ok: bool, **info) -> None:
    CHECKS.append({"name": name, "ok": bool(ok), **info})
    print(("PASS " if ok else "FAIL ") + name + (f"  {json.dumps(info, default=str)}" if info else ""), flush=True)


def gpu_used_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def proc_vram_mb(pid: int | None) -> int | None:
    if not pid:
        return None
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    for line in out.strip().splitlines():
        p, mem = (x.strip() for x in line.split(","))
        if int(p) == pid:
            return int(mem)
    return 0


def status() -> dict:
    return C.get(f"{PB}/status").json()


def render(labels: np.ndarray, session: str = "e2e", **kw) -> tuple[int, dict, float]:
    t0 = time.perf_counter()
    r = C.post(f"{PB}/render", json={"session": session, "mask": MK.encode(labels), **kw})
    return r.status_code, r.json(), (time.perf_counter() - t0) * 1000


def decode(body: dict) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(base64.b64decode(body["image"].split(",", 1)[1]))).convert("RGB"))


def save(name: str, img: np.ndarray) -> str:
    p = OUT / f"{name}.png"
    Image.fromarray(img).save(p)
    return str(p)


def stats_ms(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"n": len(xs), "median": round(statistics.median(xs), 1),
            "p95": round(xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))], 1),
            "min": round(xs[0], 1), "max": round(xs[-1], 1)}


def idx(mid: str) -> int:
    return M.INDEX[mid]


def landscape(w: int = 768, h: int = 512, lake_x: int | None = None) -> np.ndarray:
    """Sky, a mountain ridge, grass and a lake: the reference scene."""
    y, x = np.mgrid[0:h, 0:w]
    labels = np.full((h, w), idx("sky"), np.uint8)
    ridge = (0.42 * h + 0.10 * h * np.sin(x / w * 6.0) + 0.05 * h * np.sin(x / w * 17.0)).astype(int)
    labels[y >= ridge] = idx("mountains")
    labels[y >= int(0.62 * h)] = idx("grass")
    cx = w * 0.62 if lake_x is None else lake_x
    lake = ((x - cx) / (0.22 * w)) ** 2 + ((y - 0.80 * h) / (0.10 * h)) ** 2 < 1
    labels[lake] = idx("water")
    return labels


def region_mean(img: np.ndarray, labels: np.ndarray, mid: str) -> list[float]:
    m = labels == idx(mid)
    return [round(float(img[..., c][m].mean()), 1) for c in range(3)]


# -------------------------------------------------------------------- suites
def suite_basics() -> None:
    s = status()
    RESULTS["engine_status"] = {"availability": s["availability"], "gpu": s["gpu"],
                                "components": {c["id"]: c["installed"] for c in s["components"]}}
    check("models installed", s["availability"]["state"] == "ready", availability=s["availability"])
    C.post(f"{PB}/release", json={"level": "all"})
    time.sleep(2)
    base = gpu_used_mb()
    RESULTS["gpu_used_before_mb"] = base

    labels = landscape()
    code, body, wall = render(labels, final=True)
    check("cold live render (worker spawn + model load)", code == 200, wall_ms=round(wall, 1),
          timings=body.get("timings"))
    RESULTS["cold_live"] = {"wall_ms": round(wall, 1), "timings": body.get("timings")}
    img = decode(body)
    save("scene_live", img)
    sky, grass, water, mountains = (region_mean(img, labels, m) for m in ("sky", "grass", "water", "mountains"))
    RESULTS["scene_region_means"] = {"sky": sky, "grass": grass, "water": water, "mountains": mountains}
    check("scene is a real image (not flat)", float(img.std()) > 20, std=round(float(img.std()), 1))
    check("sky is brighter and bluer than grass", sum(sky) > sum(grass) and sky[2] > grass[2], sky=sky, grass=grass)
    check("grass is green-dominant", grass[1] > grass[2] and grass[1] >= grass[0] * 0.9, grass=grass)
    check("water is bluer than grass", water[2] - water[1] > grass[2] - grass[1], water=water, grass=grass)

    pid = status()["runtime"]["pid"]
    RESULTS["vram_live_loaded"] = {"worker_mb": proc_vram_mb(pid), "gpu_used_mb": gpu_used_mb()}

    # an edit changes the output where it was made
    edited = labels.copy()
    edited[:, :192][edited[:, :192] == idx("grass")] = idx("trees")
    code, b2, _ = render(edited, final=True)
    img2 = decode(b2)
    save("scene_edit_trees_left", img2)
    changed = edited != labels
    d_in = float(np.abs(img2.astype(int) - img.astype(int))[changed].mean())
    d_out = float(np.abs(img2.astype(int) - img.astype(int))[~changed].mean())
    check("painting trees changes the edited region", code == 200 and d_in > 15 and d_in > 2 * d_out,
          diff_edited=round(d_in, 1), diff_elsewhere=round(d_out, 1))


def suite_live_latency() -> None:
    sizes = [(768, 512), (512, 512), (896, 512), (512, 768), (1024, 1024)]
    out = {}
    for w, h in sizes:
        for i in range(3):  # cudnn picks kernels for a new shape
            render(landscape(w, h, lake_x=w * 0.3 + i))
        walls, infer = [], []
        for i in range(20):
            code, body, wall = render(landscape(w, h, lake_x=w * (0.3 + 0.02 * i)))
            assert code == 200, body
            walls.append(wall)
            infer.append(body["timings"]["live_ms"])
        out[f"{w}x{h}"] = {"wall_ms": stats_ms(walls), "gpu_ms": stats_ms(infer),
                           "peak_vram_mb": body.get("peak_vram_mb")}
        print(f"live {w}x{h}: {out[f'{w}x{h}']}", flush=True)
    RESULTS["live_latency"] = out
    check("live preview median under 250 ms at 768x512 (round trip)", out["768x512"]["wall_ms"]["median"] < 250,
          median=out["768x512"]["wall_ms"]["median"])


def suite_materials() -> None:
    """Each material on the lower 55 % of a sky canvas: saves a contact sheet and region colours."""
    w, h = 512, 512
    tiles, means = [], {}
    for m in M.MATERIALS:
        labels = np.full((h, w), idx("sky"), np.uint8)
        if m.id in ("sky", "clouds", "fog"):
            labels[: int(0.6 * h)] = idx(m.id)
            labels[int(0.6 * h):] = idx("grass")
            region = (slice(0, int(0.6 * h)),)
        else:
            labels[int(0.45 * h):] = idx(m.id)
            region = (slice(int(0.45 * h), h),)
        code, body, _ = render(labels, session="materials", final=True)
        assert code == 200, body
        img = decode(body)
        save(f"material_{m.id}", img)
        reg = img[region]
        means[m.id] = {"rgb": [round(float(reg[..., c].mean()), 1) for c in range(3)],
                       "std": round(float(reg.std()), 1), "live": m.live, "refine": m.refine}
        tile = Image.fromarray(img).resize((192, 192))
        ImageDraw.Draw(tile).rectangle((0, 0, 192, 18), fill=(255, 255, 255))
        ImageDraw.Draw(tile).text((4, 3), m.label + ("" if m.live == "native" else " (experimental)"), fill=(0, 0, 0))
        tiles.append(tile)
    cols = 5
    sheet = Image.new("RGB", (cols * 192, ((len(tiles) + cols - 1) // cols) * 192), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * 192, (i // cols) * 192))
    sheet.save(OUT / "materials_contact_sheet.png")
    RESULTS["materials"] = means
    g, wtr, snow, sand, dirt = (means[k]["rgb"] for k in ("grass", "water", "snow", "sand", "dirt"))
    check("materials produce different textures (grass vs water vs sand)",
          min(np.abs(np.subtract(g, wtr)).sum(), np.abs(np.subtract(g, sand)).sum(),
              np.abs(np.subtract(wtr, sand)).sum()) > 30, grass=g, water=wtr, sand=sand)
    check("snow is brighter than dirt", sum(snow) > sum(dirt) + 60, snow=snow, dirt=dirt)


def suite_refine() -> None:
    t0 = time.perf_counter()
    r = C.post(f"{PB}/warmup", json={"refine": True})
    check("refine warm-up", r.status_code == 200, status=r.status_code)
    RESULTS["refine_load_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    pid = status()["runtime"]["pid"]
    RESULTS["vram_refine_loaded"] = {"worker_mb": proc_vram_mb(pid), "gpu_used_mb": gpu_used_mb()}
    labels = landscape()
    runs = {}
    for quality, scale, n in (("draft", 1.0, 3), ("standard", 1.0, 3), ("high", 1.0, 2), ("standard", 1.5, 2),
                              ("standard", 2.0, 2)):
        walls, gpu, peaks = [], [], []
        body = {}
        for i in range(n):
            code, body, wall = render(labels, kind="refine", seed=100 + i, quality=quality, scale=scale)
            assert code == 200, body
            walls.append(wall)
            gpu.append(body["timings"]["live_ms"] + body["timings"]["refine_ms"])
            peaks.append(body.get("peak_vram_mb") or 0)
        save(f"refine_{quality}_x{scale}", decode(body))
        key = f"{quality}@{scale}x ({body['width']}x{body['height']}, {body['steps']} steps)"
        runs[key] = {"wall_ms": stats_ms(walls), "gpu_ms": stats_ms(gpu), "peak_vram_mb": max(peaks),
                     "worker_mb": proc_vram_mb(pid)}
        print(f"refine {key}: {runs[key]}", flush=True)
    RESULTS["refine_latency"] = runs
    check("refine peaks stay under 10 GB", max(r["peak_vram_mb"] for r in runs.values()) < 10_000,
          peaks={k: v["peak_vram_mb"] for k, v in runs.items()})

    a = decode(render(labels, kind="refine", seed=42, quality="draft")[1])
    b = decode(render(labels, kind="refine", seed=42, quality="draft")[1])
    c = decode(render(labels, kind="refine", seed=43, quality="draft")[1])
    same, other = float(np.abs(a.astype(int) - b).mean()), float(np.abs(a.astype(int) - c).mean())
    check("same seed reproduces, a new seed varies", same < 2 and other > 4 * max(same, 0.5),
          same_seed_diff=round(same, 2), new_seed_diff=round(other, 2))
    save("refine_seed42", a)
    save("refine_seed43", c)
    plain = decode(render(labels, kind="refine", seed=42, quality="draft", creativity=0.85)[1])
    snowy = decode(render(labels, kind="refine", seed=42, quality="draft", creativity=0.85,
                          prompt="winter, deep snow, overcast")[1])
    save("refine_seed42_c085", plain)
    save("refine_seed42_c085_winter_prompt", snowy)
    check("text guidance changes the result at creativity 0.85", float(np.abs(snowy.astype(int) - plain).mean()) > 12,
          diff=round(float(np.abs(snowy.astype(int) - plain).mean()), 1))
    s = decode(render(labels, kind="refine", seed=42, quality="draft", creativity=0.2)[1])
    live = decode(render(labels)[1])
    check("low creativity stays close to the live frame", float(np.abs(s.astype(int) - live).mean())
          < float(np.abs(plain.astype(int) - live).mean()))


def suite_cancel_and_rapid() -> None:
    labels = landscape()
    res: dict[str, object] = {}

    def slow():
        t0 = time.perf_counter()
        r = C.post(f"{PB}/render", json={"session": "cancel", "mask": MK.encode(labels), "kind": "refine",
                                         "quality": "high", "scale": 2.0})
        res["code"], res["body"], res["t_end"] = r.status_code, r.json(), time.perf_counter()

    th = threading.Thread(target=slow)
    th.start()
    time.sleep(1.5)
    t_cancel = time.perf_counter()
    n = C.post(f"{PB}/cancel", json={"session": "cancel"}).json()["cancelled"]
    th.join(120)
    latency = (res["t_end"] - t_cancel) * 1000
    check("cancelling a running refine returns promptly", res["code"] == 409 and n == 1 and latency < 1500,
          code=res["code"], cancel_latency_ms=round(latency, 1))
    RESULTS["cancel_latency_ms"] = round(latency, 1)
    code, _, _ = render(labels, session="cancel")
    check("the worker keeps serving after a cancel", code == 200)

    # rapid edits: a stroke sends a frame per pointer move
    codes: list[int] = []
    lock = threading.Lock()
    t0 = time.perf_counter()

    def one(i):
        c, _, _ = render(landscape(lake_x=200 + 10 * i), session="rapid")
        with lock:
            codes.append((i, c))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
    for i, t in enumerate(threads):
        t.start()
        time.sleep(0.015)
    for t in threads:
        t.join(60)
    total = (time.perf_counter() - t0) * 1000
    last = dict(codes)[15]
    ok = sum(1 for _, c in codes if c == 200)
    sup = sum(1 for _, c in codes if c == 409)
    check("rapid edits: stale frames are dropped, the newest always renders", last == 200 and ok + sup == 16,
          rendered=ok, superseded=sup, total_ms=round(total, 1))
    RESULTS["rapid_edits"] = {"sent": 16, "rendered": ok, "superseded": sup, "total_ms": round(total, 1)}

    # a newer refine pre-empts the running one
    res2: dict[str, object] = {}

    def refine(name, delay):
        time.sleep(delay)
        c, b, w = render(labels, session="pre", kind="refine", quality="high")
        res2[name] = (c, w)

    ts = [threading.Thread(target=refine, args=("old", 0)), threading.Thread(target=refine, args=("new", 1.0))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    check("a newer refine pre-empts the running one", res2["old"][0] == 409 and res2["new"][0] == 200,
          old=res2["old"], new=res2["new"])


def suite_release() -> None:
    C.post(f"{PB}/warmup", json={"refine": True})
    pid = status()["runtime"]["pid"]
    before = proc_vram_mb(pid)
    C.post(f"{PB}/release", json={"level": "refine"})
    time.sleep(1.5)
    after_refine = proc_vram_mb(pid)
    C.post(f"{PB}/release", json={"level": "all"})
    time.sleep(2)
    gone = gpu_used_mb()
    RESULTS["vram_release"] = {"worker_with_refine_mb": before, "worker_live_only_mb": after_refine,
                               "gpu_used_after_stop_mb": gone, "gpu_used_before_tests_mb": RESULTS["gpu_used_before_mb"]}
    check("releasing refine frees most of its memory", after_refine is not None and before - after_refine > 2000,
          before=before, after=after_refine)
    check("stopping the worker returns the VRAM", gone <= RESULTS["gpu_used_before_mb"] + 150,
          used=gone, baseline=RESULTS["gpu_used_before_mb"])


def suite_coexist() -> None:
    """Chat and image generation keep working while the paintbrush is loaded."""
    labels = landscape()
    assert render(labels)[0] == 200
    images = [m for m in C.get("/v1/models").json() if m.get("category") == "image" and m.get("installed") is not False]
    image = next((m for m in images if "turbo" in m["id"]), images[0] if images else None)
    chats = C.get("/v1/llm/models").json()
    chat_ref = os.environ.get("SARNESS_CHAT_MODEL") or (chats[0]["ref"] if chats else None)
    RESULTS["coexist_models"] = {"image": image and image["id"], "chat": chat_ref}
    if chat_ref:
        conv = C.post("/v1/chat/conversations", json={"title": "paintbrush coexistence test", "model_ref": chat_ref}).json()
        text = ""
        with C.stream("POST", f"/v1/chat/conversations/{conv['id']}/generate",
                      json={"content": "Reply with the single word: ready"}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "delta":
                        text += ev.get("content") or ""
        C.delete(f"/v1/chat/conversations/{conv['id']}")
        worker = status()["runtime"]["worker"]
        check("chat answers while the paintbrush is loaded", bool(text.strip()) and worker, model=chat_ref,
              reply=text[:40], paintbrush_worker_alive=worker)
        code, _, wall = render(labels)
        check("paintbrush still renders after a chat reply", code == 200, wall_ms=round(wall, 1))
    else:
        check("chat answers while the paintbrush is loaded", False, reason="no chat endpoint models listed")
    if image:
        r = C.post("/v1/jobs", json={"kind": "image", "simple": {
            "model": image["id"], "prompt": "a lighthouse on a cliff at dusk, photograph", "steps": 8,
            "width": 512, "height": 512, "seed": 3}})
        assert r.status_code == 200, r.text
        jid = r.json()["id"]
        t0 = time.time()
        yielded = False
        while True:
            j = C.get(f"/v1/jobs/{jid}").json()
            st = status()["runtime"]
            yielded = yielded or not st["worker"]
            if j["status"] in ("completed", "failed", "cancelled") or time.time() - t0 > 900:
                break
            time.sleep(0.5)
        check("an image job runs while the paintbrush was loaded", j["status"] == "completed",
              status=j["status"], model=image["id"], error=(j.get("error") or {}).get("message"))
        check("the paintbrush gave the GPU to the job", yielded or bool(status()["runtime"]["gpu_busy"]))
        time.sleep(11)  # the hand-over hold
        code, body, wall = render(labels)
        check("paintbrush renders again after the job", code == 200, wall_ms=round(wall, 1))
    else:
        check("an image job runs while the paintbrush was loaded", False, reason="no image model installed")


def main() -> int:
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="paintbrush-e2e-out")
    ap.add_argument("--coexist", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    OUT = Path(a.out)
    OUT.mkdir(parents=True, exist_ok=True)
    suites = {"basics": suite_basics, "live": suite_live_latency, "materials": suite_materials,
              "refine": suite_refine, "cancel": suite_cancel_and_rapid, "release": suite_release}
    if a.coexist:
        suites["coexist"] = suite_coexist
    t0 = time.time()
    for name, fn in suites.items():
        if a.only and name not in a.only:
            continue
        print(f"== {name}", flush=True)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check(f"{name} suite crashed", False, error=repr(exc))
    RESULTS["checks"] = CHECKS
    RESULTS["elapsed_s"] = round(time.time() - t0, 1)
    RESULTS["final_status"] = {k: v for k, v in status().items() if k in ("runtime", "stats", "gpu")}
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str))
    failed = [c["name"] for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
