"""Simple-mode ⇄ workflow conversion.

Simple mode is not a separate code path: every simple generation is compiled to
a real graph by :func:`image_workflow` / :func:`video_workflow` and executed by
the same executor. :func:`to_simple` reverses the mapping when — and only when —
the graph is structurally identical to what simple mode would build.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .types import WORKFLOW_FORMAT, WORKFLOW_VERSION

IMAGE_DEFAULTS: dict[str, Any] = {
    "model": "", "prompt": "", "negative_prompt": "", "width": 1024, "height": 1024, "batch_size": 1,
    "steps": 30, "guidance": 6.0, "seed": -1, "scheduler": "default", "denoise": 0.65,
    "reference_image": None, "mask_image": None, "loras": [], "vae": None, "memory_mode": "auto",
    "upscale": None, "format": "png", "clip_skip": 0, "style": "none", "extra_references": [],
}

VIDEO_DEFAULTS: dict[str, Any] = {
    "model": "", "prompt": "", "negative_prompt": None, "width": 832, "height": 480, "frames": 33,
    "fps": 16, "steps": 30, "guidance": 5.0, "seed": -1, "flow_shift": 3.0, "start_image": None,
    "end_image": None, "memory_mode": "auto", "format": "mp4",
}


class _Builder:
    def __init__(self, kind: str, name: str):
        self.g: dict[str, Any] = {"format": WORKFLOW_FORMAT, "version": WORKFLOW_VERSION, "name": name,
                                  "kind": kind, "nodes": [], "edges": [], "groups": []}

    def add(self, nid: str, type_: str, params: dict[str, Any], col: float, row: float, label: str | None = None) -> str:
        n = {"id": nid, "type": type_, "params": params,
             "position": {"x": round(80 + col * 340), "y": round(60 + row * 300)}}
        if label:
            n["label"] = label
        self.g["nodes"].append(n)
        return nid

    def link(self, src: str, out: str, dst: str, inp: str) -> None:
        self.g["edges"].append({"id": f"e_{src}_{out}__{dst}_{inp}", "source": src, "source_output": out,
                                "target": dst, "target_input": inp})


def image_workflow(simple: dict[str, Any]) -> dict[str, Any]:
    p = {**IMAGE_DEFAULTS, **{k: v for k, v in simple.items() if v is not None or k in ("reference_image", "mask_image", "vae", "upscale")}}
    b = _Builder("image", "Image generation")
    b.add("model", "loader.image_model", {"model": p["model"], "vae": p["vae"] or "", "memory_mode": p["memory_mode"]}, 0, 0)
    model_out = "model"
    for i, lora in enumerate(p["loras"] or []):
        nid = b.add(f"lora_{i}", "loader.lora", {"lora": lora["id"], "strength": float(lora.get("strength", 1.0))}, 0, 1 + i)
        b.link(model_out, "model", nid, "model")
        model_out = nid
    b.add("prompt", "prompt.encode", {"prompt": p["prompt"], "negative_prompt": p["negative_prompt"],
                                      "style": p["style"] or "none", "clip_skip": int(p["clip_skip"])}, 1, 0)
    b.link(model_out, "model", "prompt", "model")

    sampler_params = {"seed": int(p["seed"]), "steps": int(p["steps"]), "guidance": float(p["guidance"]),
                      "denoise": 1.0, "guidance_rescale": 0.0}
    b.add("sampler", "sampler.image", sampler_params, 2, 0)
    b.link(model_out, "model", "sampler", "model")
    b.link("prompt", "conditioning", "sampler", "conditioning")

    if p["reference_image"]:
        b.add("ref", "image.load", {"file": p["reference_image"]}, 0, 1.25)
        b.add("ref_resize", "image.resize", {"width": int(p["width"]), "height": int(p["height"]), "mode": "fill",
                                             "multiple_of": 8}, 0, 2.3)
        b.link("ref", "image", "ref_resize", "image")
        b.add("encode", "vae.encode", {"sample": False, "seed": 0}, 1, 2.3)
        b.link(model_out, "model", "encode", "model")
        b.link("ref_resize", "image", "encode", "image")
        b.link("encode", "latent", "sampler", "latent")
        sampler_params["denoise"] = float(p["denoise"])
        if p["mask_image"]:
            b.add("mask", "mask.load", {"file": p["mask_image"], "channel": "luminance"}, 0, 3.2)
            b.link("mask", "mask", "sampler", "mask")
        # Extra references for multi-image editing models (the reference image above is the first).
        for i, ref in enumerate((p["extra_references"] or [])[:2]):
            nid = b.add(f"ref_{i + 2}", "image.load", {"file": ref}, 1, 3.2 + i, f"Reference {i + 2}")
            b.link(nid, "image", "sampler", f"reference_{i + 2}")
    else:
        b.add("latent", "latent.empty", {"width": int(p["width"]), "height": int(p["height"]),
                                         "batch_size": int(p["batch_size"])}, 1, 1.25)
        b.link("latent", "latent", "sampler", "latent")

    if p["scheduler"] and p["scheduler"] != "default":
        b.add("scheduler", "scheduler.select", {"name": p["scheduler"], "timestep_spacing": "default"}, 2, 2.1)
        b.link("scheduler", "scheduler", "sampler", "scheduler")

    b.add("decode", "vae.decode", {"tiled": False}, 3, 0)
    b.link(model_out, "model", "decode", "model")
    b.link("sampler", "latent", "decode", "latent")
    image_src = "decode"
    if p["upscale"] and p["upscale"].get("model"):
        b.add("upscaler", "loader.upscaler", {"model": p["upscale"]["model"]}, 3, 1)
        b.add("upscale", "upscale.image", {"tile": 512, "output_scale": float(p["upscale"].get("factor") or 0)}, 4, 1)
        b.link("upscaler", "upscaler", "upscale", "upscaler")
        b.link("decode", "image", "upscale", "image")
        image_src = "upscale"
    b.add("save", "output.save_image", {"format": p["format"], "quality": 95}, 5 if image_src == "upscale" else 4, 0)
    b.link(image_src, "image", "save", "image")
    b.g["simple"] = {"kind": "image", **p}
    return b.g


def video_workflow(simple: dict[str, Any]) -> dict[str, Any]:
    from .nodes.video import WAN_NEGATIVE

    p = {**VIDEO_DEFAULTS, **{k: v for k, v in simple.items() if v is not None or k in ("start_image", "end_image")}}
    neg = p["negative_prompt"] if p["negative_prompt"] is not None else WAN_NEGATIVE
    b = _Builder("video", "Video generation")
    b.add("model", "loader.video_model", {"model": p["model"], "memory_mode": p["memory_mode"]}, 0, 0)
    b.add("prompt", "prompt.encode_video", {"prompt": p["prompt"], "negative_prompt": neg}, 1, 0)
    b.link("model", "model", "prompt", "model")
    b.add("sampler", "sampler.video", {
        "width": int(p["width"]), "height": int(p["height"]), "frames": int(p["frames"]), "fps": float(p["fps"]),
        "seed": int(p["seed"]), "steps": int(p["steps"]), "guidance": float(p["guidance"]),
        "flow_shift": float(p["flow_shift"])}, 2, 0)
    b.link("model", "model", "sampler", "model")
    b.link("prompt", "conditioning", "sampler", "conditioning")
    if p["start_image"]:
        b.add("start", "image.load", {"file": p["start_image"]}, 1, 1, "Start frame")
        b.link("start", "image", "sampler", "start_image")
    if p["end_image"]:
        b.add("end", "image.load", {"file": p["end_image"]}, 1, 2, "End frame")
        b.link("end", "image", "sampler", "end_image")
    b.add("save", "output.save_video", {"format": p["format"], "quality": 8, "fps": 0}, 3, 0)
    b.link("sampler", "video", "save", "video")
    b.g["simple"] = {"kind": "video", **p}
    return b.g


# ---------------------------------------------------------------- reverse map
def _signature(g: dict[str, Any]) -> Counter:
    types = {n["id"]: n["type"] for n in g.get("nodes", [])}
    sig = Counter()
    for n in g.get("nodes", []):
        ins = sorted((e["target_input"], types.get(e["source"], "?"), e["source_output"])
                     for e in g.get("edges", []) if e["target"] == n["id"])
        sig[(n["type"], tuple(ins))] += 1
    return sig


def _find(g, type_):
    return [n for n in g.get("nodes", []) if n["type"] == type_]


def to_simple(g: dict[str, Any]) -> dict[str, Any]:
    """Returns ``{"representable": bool, "simple": {...} | None, "reasons": [...]}``."""
    kind = g.get("kind", "image")
    reasons: list[str] = []
    allowed = ({"loader.image_model", "loader.lora", "prompt.encode", "sampler.image", "latent.empty", "image.load",
                "image.resize", "vae.encode", "mask.load", "scheduler.select", "vae.decode", "loader.upscaler",
                "upscale.image", "output.save_image"} if kind == "image" else
               {"loader.video_model", "prompt.encode_video", "sampler.video", "image.load", "output.save_video"})
    extra = sorted({n["type"] for n in g.get("nodes", [])} - allowed)
    if extra:
        reasons.append("uses nodes that simple mode cannot show: " + ", ".join(extra))
    try:
        simple = _extract_image(g) if kind == "image" else _extract_video(g)
    except _NotSimple as exc:
        reasons.append(str(exc))
        simple = None
    if simple is not None and not reasons:
        rebuilt = image_workflow(simple) if kind == "image" else video_workflow(simple)
        if _signature(rebuilt) != _signature(g):
            reasons.append("its connections differ from the standard simple-mode pipeline")
    ok = not reasons
    return {"representable": ok, "simple": simple if ok else None, "reasons": reasons}


class _NotSimple(Exception):
    pass


def _one(g, type_, required=True):
    found = _find(g, type_)
    if len(found) > 1:
        raise _NotSimple(f"has {len(found)} '{type_}' nodes")
    if not found:
        if required:
            raise _NotSimple(f"is missing a '{type_}' node")
        return None
    return found[0].get("params") or {}


def _extract_image(g) -> dict[str, Any]:
    m = _one(g, "loader.image_model")
    pr = _one(g, "prompt.encode")
    s = _one(g, "sampler.image")
    out = _one(g, "output.save_image")
    lat = _one(g, "latent.empty", required=False)
    rs = _one(g, "image.resize", required=False)
    mask = _one(g, "mask.load", required=False)
    sch = _one(g, "scheduler.select", required=False)
    up_l = _one(g, "loader.upscaler", required=False)
    up = _one(g, "upscale.image", required=False)
    if sch and sch.get("timestep_spacing", "default") != "default":
        raise _NotSimple("uses a custom timestep spacing")
    if (s.get("guidance_rescale") or 0) != 0:
        raise _NotSimple("uses guidance rescale")
    loras = []
    # Preserve chain order by walking edges from the loader.
    edges = g.get("edges", [])
    nodes = {n["id"]: n for n in g.get("nodes", [])}
    cur = next(n["id"] for n in g["nodes"] if n["type"] == "loader.image_model")
    seen = set()
    while True:
        nxt = [e["target"] for e in edges if e["source"] == cur and nodes.get(e["target"], {}).get("type") == "loader.lora"]
        if not nxt or nxt[0] in seen:
            break
        cur = nxt[0]
        seen.add(cur)
        lp = nodes[cur].get("params") or {}
        loras.append({"id": lp.get("lora"), "strength": lp.get("strength", 1.0)})
    if len(seen) != len(_find(g, "loader.lora")):
        raise _NotSimple("LoRAs are not chained in a single line")
    # The reference image feeds the resize node; extra references (editing models) feed the sampler directly.
    loads = {n["id"]: n.get("params") or {} for n in g["nodes"] if n["type"] == "image.load"}
    ref, extra, used = None, [], set()
    for e in edges:
        if e["source"] in loads and nodes.get(e["target"], {}).get("type") == "image.resize":
            ref = loads[e["source"]]
            used.add(e["source"])
    for i in (2, 3):
        src = next((e["source"] for e in edges if e["source"] in loads and e["target_input"] == f"reference_{i}"), None)
        if src:
            extra.append(loads[src].get("file"))
            used.add(src)
    if set(loads) - used:
        raise _NotSimple("loads images that simple mode cannot place")
    width = (rs or lat or {}).get("width", 1024)
    height = (rs or lat or {}).get("height", 1024)
    if rs and (rs.get("mode", "fill") != "fill" or rs.get("multiple_of", 8) != 8):
        raise _NotSimple("resizes the reference image with custom settings")
    return {
        "model": m.get("model"), "vae": m.get("vae") or None, "memory_mode": m.get("memory_mode", "auto"),
        "prompt": pr.get("prompt", ""), "negative_prompt": pr.get("negative_prompt", ""),
        "style": pr.get("style", "none"), "clip_skip": pr.get("clip_skip", 0),
        "width": width, "height": height, "batch_size": (lat or {}).get("batch_size", 1),
        "steps": s.get("steps"), "guidance": s.get("guidance"), "seed": s.get("seed", -1),
        "denoise": s.get("denoise", 1.0) if ref else IMAGE_DEFAULTS["denoise"],
        "scheduler": (sch or {}).get("name", "default"),
        "reference_image": (ref or {}).get("file"), "mask_image": (mask or {}).get("file"),
        "loras": loras, "extra_references": extra,
        "upscale": {"model": up_l.get("model"), "factor": (up or {}).get("output_scale", 0)} if up_l else None,
        "format": out.get("format", "png"),
    }


def _extract_video(g) -> dict[str, Any]:
    m = _one(g, "loader.video_model")
    pr = _one(g, "prompt.encode_video")
    s = _one(g, "sampler.video")
    out = _one(g, "output.save_video")
    if (out.get("fps") or 0) != 0 or out.get("quality", 8) != 8:
        raise _NotSimple("uses custom encoder settings")
    nodes = {n["id"]: n for n in g.get("nodes", [])}
    start = end = None
    for e in g.get("edges", []):
        src = nodes.get(e["source"])
        if src and src["type"] == "image.load":
            if e["target_input"] == "start_image":
                start = (src.get("params") or {}).get("file")
            elif e["target_input"] == "end_image":
                end = (src.get("params") or {}).get("file")
    return {
        "model": m.get("model"), "memory_mode": m.get("memory_mode", "auto"),
        "prompt": pr.get("prompt", ""), "negative_prompt": pr.get("negative_prompt"),
        "width": s.get("width"), "height": s.get("height"), "frames": s.get("frames"), "fps": s.get("fps"),
        "steps": s.get("steps"), "guidance": s.get("guidance"), "seed": s.get("seed", -1),
        "flow_shift": s.get("flow_shift", 3.0), "start_image": start, "end_image": end,
        "format": out.get("format", "mp4"),
    }


def summarize(g: dict[str, Any]) -> dict[str, Any]:
    """Best-effort human summary of a graph for job titles and library cards."""
    summary: dict[str, Any] = {}
    for n in g.get("nodes", []):
        p = n.get("params") or {}
        t = n["type"]
        if t in ("prompt.encode", "prompt.encode_video") and "prompt" not in summary:
            summary["prompt"] = p.get("prompt", "")
            summary["negative_prompt"] = p.get("negative_prompt", "")
        elif t in ("loader.image_model", "loader.video_model") and "model" not in summary:
            summary["model"] = p.get("model")
        elif t in ("sampler.image", "sampler.video") and "seed" not in summary:
            summary.update({k: p.get(k) for k in ("seed", "steps", "guidance", "width", "height", "frames", "fps") if k in p})
        elif t == "latent.empty":
            summary.setdefault("width", p.get("width"))
            summary.setdefault("height", p.get("height"))
    return summary
