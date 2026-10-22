"""End-to-end image families (Qwen-Image-2512 / Edit-2511, Z-Image, FLUX.2): modes, guidance styles and the
simple-mode graph, with fake pipelines in place of the diffusers ones."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Ctx:
    def __init__(self):
        self.notes, self.steps = {}, []

    def annotate(self, **kw):
        self.notes.update(kw)

    def progress(self, step, total, text=None, preview=None):
        self.steps.append((step, total))


class _Pipe:
    def __init__(self, name="base"):
        self.name, self.calls = name, []

    def __call__(self, **kw):
        from PIL import Image

        self.calls.append(kw)
        kw["callback_on_step_end"](self, 0, 0, {})
        size = (kw["width"], kw["height"]) if kw.get("width") else kw["image"].size
        return SimpleNamespace(images=[Image.new("RGB", size)] * kw.get("num_images_per_prompt", 1))


def _setup(monkeypatch, family, caps=None):
    pytest.importorskip("torch")
    from sarness_engine.runtime.models import ModelHandle
    from sarness_engine.workflow.nodes import diffusion as d

    variants: dict[str | None, _Pipe] = {None: _Pipe()}

    def native_pipe(handle, cls_name):
        return variants.setdefault(cls_name, _Pipe(cls_name))

    monkeypatch.setattr(d, "_native_pipe", native_pipe)
    info = {"name": family, "limits": {"multiple": 16}, "capabilities": caps or {}}
    h = ModelHandle(f"image:{family}", family, family, info, variants[None], "gpu")
    return d, h, variants


def _cond(d, h, ctx, prompt="a red bicycle", negative="", style="none"):
    return d.encode_prompt(ctx, {"model": h}, {"prompt": prompt, "negative_prompt": negative, "style": style,
                                               "clip_skip": 0})["conditioning"]


PARAMS = {"seed": 3, "steps": 20, "guidance": 4.0, "denoise": 0.6, "guidance_rescale": 0.0}


def _img(w=640, h=480):
    from PIL import Image

    return Image.new("RGB", (w, h), "white")


def test_qwen_image_2512_runs_txt2img_img2img_and_inpaint(monkeypatch):
    d, h, variants = _setup(monkeypatch, "qwenimage")
    ctx = _Ctx()
    cond = _cond(d, h, ctx)
    d.image_sampler(ctx, {"model": h, "conditioning": cond,
                          "latent": {"samples": None, "width": 1328, "height": 1328, "batch": 1}}, dict(PARAMS))
    call = variants[None].calls[-1]
    assert call["true_cfg_scale"] == 4.0 and call["negative_prompt"] == " " and "guidance_scale" not in call

    enc = d.vae_encode(ctx, {"model": h, "image": [_img()]}, {"sample": False, "seed": 0})["latent"]
    assert enc["samples"] is None and enc["source"][0].size == (640, 480)
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": enc}, dict(PARAMS))
    call = variants["QwenImageImg2ImgPipeline"].calls[-1]
    assert call["strength"] == 0.6 and call["image"].size == (640, 480)
    assert ctx.notes["mode"] == "img2img" and ctx.steps[-1][1] == 12, "progress counts only the denoised steps"

    from PIL import Image

    mask = [Image.new("L", (320, 240), 255)]
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": enc, "mask": mask}, dict(PARAMS))
    call = variants["QwenImageInpaintPipeline"].calls[-1]
    assert call["mask_image"].size == (640, 480), "mask resized to the source"
    assert ctx.notes["mode"] == "inpaint"


def test_edit_model_takes_the_reference_and_extra_references(monkeypatch):
    from sarness_engine.workflow.types import WorkflowError

    d, h, variants = _setup(monkeypatch, "qwenimage-edit", {"references": 3})
    ctx = _Ctx()
    cond = _cond(d, h, ctx, "put the cat from image 2 on the sofa")
    enc = d.vae_encode(ctx, {"model": h, "image": [_img()]}, {"sample": False, "seed": 0})["latent"]
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": enc, "reference_2": [_img(300, 300)],
                          "reference_3": [_img(200, 200)]}, dict(PARAMS))
    call = variants[None].calls[-1]
    assert [im.size for im in call["image"]] == [(640, 480), (300, 300), (200, 200)]
    assert "strength" not in call and ctx.notes["mode"] == "edit" and ctx.notes["references"] == 3
    with pytest.raises(WorkflowError, match="does not support text-to-image"):
        d.image_sampler(ctx, {"model": h, "conditioning": cond,
                              "latent": {"samples": None, "width": 512, "height": 512, "batch": 1}}, dict(PARAMS))


def test_flux2_klein_uses_embedded_guidance_and_no_negative_prompt(monkeypatch):
    d, h, variants = _setup(monkeypatch, "flux2klein", {"references": 4})
    ctx = _Ctx()
    cond = _cond(d, h, ctx, negative="blurry")
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "reference_2": [_img()],
                          "latent": {"samples": None, "width": 1024, "height": 1024, "batch": 1}},
                    {**PARAMS, "guidance": 1.0, "steps": 4})
    call = variants[None].calls[-1]
    assert call["guidance_scale"] == 1.0 and "negative_prompt" not in call and "true_cfg_scale" not in call
    assert len(call["image"]) == 1, "extra references guide a new image"
    enc = d.vae_encode(ctx, {"model": h, "image": [_img()]}, {"sample": False, "seed": 0})["latent"]
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": enc}, dict(PARAMS))
    assert ctx.notes["mode"] == "edit", "a reference image is edited, not used as an img2img start"


def test_z_image_turbo_runs_without_cfg_and_base_with_negative(monkeypatch):
    d, h, variants = _setup(monkeypatch, "zimage")
    ctx = _Ctx()
    cond = _cond(d, h, ctx, negative="cartoon")
    lat = {"samples": None, "width": 1024, "height": 1024, "batch": 2}
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": lat}, {**PARAMS, "guidance": 0.0, "steps": 9})
    call = variants[None].calls[-1]
    assert call["guidance_scale"] == 0.0 and "negative_prompt" not in call and call["num_images_per_prompt"] == 2
    d.image_sampler(ctx, {"model": h, "conditioning": cond, "latent": lat}, dict(PARAMS))
    assert variants[None].calls[-1]["negative_prompt"] == "cartoon"


def test_iphone_style_wraps_the_prompt_for_every_family(monkeypatch):
    from sarness_engine.workflow.styles import STYLES, apply_style

    d, h, variants = _setup(monkeypatch, "qwenimage")
    ctx = _Ctx()
    cond = _cond(d, h, ctx, "a woman laughing at a kitchen table", "dark", style="iphone")
    assert "a woman laughing at a kitchen table" in cond["prompt"] and "iPhone" in cond["prompt"]
    assert cond["negative_prompt"].startswith("dark, ") and "plastic skin" in cond["negative_prompt"]
    assert ctx.notes["prompt"] == "a woman laughing at a kitchen table", "history keeps what the user typed"
    assert ctx.notes["style"] == "iphone"
    assert apply_style("none", "x", "y") == ("x", "y") and apply_style("unknown", "x", "") == ("x", "")
    assert set(STYLES) >= {"iphone"}


def test_simple_mode_round_trips_style_and_extra_references():
    from sarness_engine.workflow.templates import image_workflow, to_simple

    simple = {"model": "qwen-image-edit-2511", "prompt": "swap the jacket", "style": "iphone",
              "reference_image": "f_ref", "extra_references": ["f_2", "f_3"], "steps": 40, "guidance": 4.0}
    g = image_workflow(simple)
    targets = {e["target_input"] for e in g["edges"] if e["target"] == "sampler"}
    assert {"reference_2", "reference_3"} <= targets
    back = to_simple(g)
    assert back["representable"], back["reasons"]
    assert back["simple"]["style"] == "iphone"
    assert back["simple"]["reference_image"] == "f_ref" and back["simple"]["extra_references"] == ["f_2", "f_3"]
    plain = to_simple(image_workflow({"model": "z-image-turbo", "prompt": "x"}))
    assert plain["representable"] and plain["simple"]["extra_references"] == [] and plain["simple"]["style"] == "none"


def test_hunyuan_video_sets_guidance_on_its_guider_and_crops_the_start_frame():
    pytest.importorskip("torch")
    import numpy as np

    from sarness_engine.runtime.models import ModelHandle
    from sarness_engine.workflow.nodes import video as v

    class Sched:
        def step(self, *a, **kw):
            return a

    class Pipe:
        guider = SimpleNamespace(guidance_scale=6.0)
        target_size = 960

        def __init__(self):
            self.scheduler = Sched()

        def __call__(self, **kw):
            assert "callback_on_step_end" not in kw, "the HunyuanVideo 1.5 pipelines take no step callback"
            self.kw = kw
            self.scheduler.step(0, 0, 0)
            if "image" in kw:  # the bucket closest to the frame's aspect, with an area of target_size²
                iw, ih = kw["image"].size
                w = round((self.target_size ** 2 * iw / ih) ** 0.5 / 16) * 16
                h = round((self.target_size ** 2 * ih / iw) ** 0.5 / 16) * 16
            else:
                w, h = kw["width"], kw["height"]
            return SimpleNamespace(frames=[np.zeros((kw["num_frames"], h, w, 3), dtype=np.float32)])

    for family, start in (("hunyuanvideo15", None), ("hunyuanvideo15-i2v", [_img(1920, 1080)])):
        pipe = Pipe()
        caps = {"modes": ["i2v"] if start else ["t2v"], "start_frame": bool(start)}
        h = ModelHandle("video:h", family, family, {"name": "HY", "capabilities": caps,
                                                    "limits": {"multiple": 16, "frame_step": 4}}, pipe, "gpu")
        ctx = _Ctx()
        cond = v.encode_video_prompt(ctx, {"model": h}, {"prompt": "a dog runs", "negative_prompt": "blur"})["conditioning"]
        assert cond == {"family": family, "prompt": "a dog runs", "negative_prompt": "blur"}
        inputs = {"model": h, "conditioning": cond, **({"start_image": start} if start else {})}
        out = v.video_sampler(ctx, inputs, {"width": 848, "height": 480, "frames": 122, "fps": 24, "seed": 1, "steps": 30,
                                            "guidance": 5.0, "flow_shift": 3.0})["video"]
        assert pipe.guider.guidance_scale == 5.0
        assert pipe.kw["num_frames"] == 121, "snapped to 4k+1"
        assert len(out["frames"]) == 121 and out["frames"][0].size == (848, 480), "not the checkpoint's 720p"
        assert ("height" in pipe.kw) == (start is None), "image-to-video takes its size from the start frame"
        assert pipe.target_size == (640 if start else 960)
        assert ctx.steps[-1][:2] == (1, 30), "progress comes from the scheduler's steps"
        assert "step" not in vars(pipe.scheduler), "the scheduler is restored afterwards"


def test_evicting_a_model_to_make_room_notifies_the_node_cache(monkeypatch):
    torch = pytest.importorskip("torch")

    from sarness_engine.runtime import models as rm
    from sarness_engine.workflow.executor import NodeCache

    # A cached loader output kept the handle of an evicted model, whose weights were already dropped:
    # the next img2img job on that model then ran on empty (meta) tensors instead of reloading it.
    mm, cache = rm.ModelManager(), NodeCache()
    mm.on_evict.append(lambda key: cache.clear())
    mm._cache["image:z"] = rm._Loaded(torch.nn.Linear(2, 2), 1000, "image", "gpu")
    mm._cache["image:q"] = rm._Loaded(torch.nn.Linear(2, 2), 1000, "image", "gpu")
    cache.put("loader:z", {"model": "handle of image:z"})
    monkeypatch.setattr(rm, "free_vram_mb", lambda: 0)
    mm.make_room(50_000, keep={"image:q"})
    assert mm.loaded_keys() == ["image:q"] and len(cache) == 0


def test_packed_attention_matches_the_dense_padding_mask():
    torch = pytest.importorskip("torch")
    import torch.nn.functional as F

    from sarness_engine.runtime.attention import packed, padding_vector

    calls = []

    def sdpa(q, k, v, attn_mask=None, **kw):  # (batch, seq, heads, dim) like diffusers' dispatch_attention_fn
        calls.append(attn_mask)
        o = F.scaled_dot_product_attention(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), attn_mask=attn_mask)
        return o.transpose(1, 2)

    torch.manual_seed(0)
    q, k, v = (torch.randn(2, 14, 3, 8) for _ in range(3))
    # Built exactly as diffusers' HunyuanVideo15AttnProcessor2_0 does: video tokens, then text tokens with padding.
    text = torch.tensor([[1] * 4 + [0] * 2, [1] * 6], dtype=torch.bool)
    valid = F.pad(text, (14 - text.shape[1], 0), value=True)
    m1 = valid.view(2, 1, 1, 14).repeat(1, 1, 14, 1)
    mask = m1 & m1.transpose(2, 3)
    assert torch.equal(padding_vector(mask, 14), valid)

    ref = sdpa(q, k, v, attn_mask=mask)
    calls.clear()
    out = packed(sdpa)(q, k, v, attn_mask=mask, dropout_p=0.0)
    assert all(c is None for c in calls), "the wrapped kernel never sees a mask"
    assert torch.allclose(out[valid], ref[valid], atol=1e-5)
    assert not out[~valid].any(), "padded positions are zeros"
    # Any other mask passes through untouched.
    causal = torch.ones(14, 14, dtype=torch.bool).tril().view(1, 1, 14, 14).expand(2, 1, 14, 14)
    assert padding_vector(causal, 14) is None
    assert torch.allclose(packed(sdpa)(q, k, v, attn_mask=causal), sdpa(q, k, v, attn_mask=causal))
    assert packed(packed(sdpa)) is not sdpa and getattr(packed(sdpa), "_sarness_packed")


def test_unified_memory_drops_model_files_from_the_page_cache_and_never_offloads(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    import os

    from sarness_engine.runtime import models as rm

    (tmp_path / "transformer").mkdir()
    for name in ("model_index.json", "transformer/a.safetensors", "transformer/b.safetensors"):
        (tmp_path / name).write_bytes(b"x")
    advised = []
    monkeypatch.setattr(os, "posix_fadvise", lambda fd, off, n, advice: advised.append(advice))
    mm = rm.ModelManager()
    monkeypatch.setattr(rm, "unified_memory", lambda: False)
    mm._store("image:a", rm._Loaded(torch.nn.Linear(1, 1), 1, "image", "gpu"), str(tmp_path))
    assert advised == [], "discrete GPUs keep the page cache"
    monkeypatch.setattr(rm, "unified_memory", lambda: True)
    mm._store("image:b", rm._Loaded(torch.nn.Linear(1, 1), 1, "image", "gpu"), str(tmp_path))
    assert advised == [os.POSIX_FADV_DONTNEED] * 3 and mm.loaded_keys() == ["image:a", "image:b"]

    monkeypatch.setattr(rm, "cuda_available", lambda: True)
    monkeypatch.setattr(mm, "make_room", lambda need, keep=None: None)
    assert [mm._choose_mode(m, 50_000, set()) for m in ("auto", "gpu", "offload", "sequential")] == ["gpu"] * 4


def test_task_variants_keep_the_loaded_pipelines_dtype(monkeypatch):
    torch = pytest.importorskip("torch")
    diffusers = pytest.importorskip("diffusers")

    from sarness_engine.workflow.nodes import diffusion as d

    seen = []

    class Variant:
        @classmethod
        def from_pipe(cls, base, **kw):
            seen.append(kw)
            return SimpleNamespace(set_progress_bar_config=lambda **k: None)

    monkeypatch.setattr(diffusers, "QwenImageInpaintPipeline", Variant, raising=False)
    base = SimpleNamespace(transformer=SimpleNamespace(dtype=torch.bfloat16))
    h = SimpleNamespace(pipe=base)
    first = d._native_pipe(h, "QwenImageInpaintPipeline")
    assert d._native_pipe(h, "QwenImageInpaintPipeline") is first and d._native_pipe(h, None) is base
    # Without an explicit dtype diffusers casts the shared weights to float32 in place: twice the memory.
    assert seen == [{"dtype": torch.bfloat16}]
