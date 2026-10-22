"""Video generation and frame-processing nodes.

VIDEO values are ``{"frames": list[PIL.Image], "fps": float}``.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageOps

from ..registry import node
from ..types import FLOAT, IMAGE, INT, VIDEO, VIDEO_CONDITIONING, VIDEO_MODEL, Param, Socket, WorkflowError
VIDEO_MEMORY_MODES = [
    {"value": "auto", "label": "Auto"},
    {"value": "gpu", "label": "GPU (needs ~16 GB+)"},
    {"value": "split", "label": "Split (text encoder streamed)"},
    {"value": "offload", "label": "Offload (transformer streamed too)"},
    {"value": "sequential", "label": "Sequential (minimum VRAM, slowest)"},
]

VIDEO_FAMILIES = ["wan", "wan22-ti2v", "ltx2", "minimaxh3", "hunyuanvideo15", "hunyuanvideo15-i2v", "gateway"]
# Families whose pipeline encodes the prompt itself (the prompt node passes the text through).
_TEXT_PROMPT_FAMILIES = {"ltx2", "hunyuanvideo15", "hunyuanvideo15-i2v"}
WAN_NEGATIVE = ("blurry, low quality, worst quality, jpeg artifacts, static, still frame, overexposed, "
                "distorted, deformed, extra limbs, fused fingers, messy background, watermark, subtitles, text")


def _info(ctx, model_id):
    info = ctx.services.model_info(model_id)
    if info is None:
        raise WorkflowError(f"model '{model_id}' is not installed on the server")
    return info


@node("loader.video_model", title="Load Video Model", category="Loaders", gpu=True,
      outputs=[Socket("model", VIDEO_MODEL)],
      params=[Param("model", "model", "", model_category="video", model_family=VIDEO_FAMILIES),
              Param("memory_mode", "enum", "auto", options=VIDEO_MEMORY_MODES, advanced=True)])
def load_video_model(ctx, inputs, params):
    info = _info(ctx, params["model"])
    ctx.progress(0, 1, "Loading video model")
    handle = ctx.services.models.video_pipeline(info, params["memory_mode"])
    ctx.annotate(model_id=info["id"], model_name=info.get("name"), memory_mode=handle.memory_mode)
    return {"model": handle}


@node("prompt.encode_video", title="Encode Video Prompt", category="Prompt", gpu=True,
      inputs=[Socket("model", VIDEO_MODEL)], outputs=[Socket("conditioning", VIDEO_CONDITIONING)],
      params=[Param("prompt", "text", "", socket=True),
              Param("negative_prompt", "text", WAN_NEGATIVE, socket=True)])
def encode_video_prompt(ctx, inputs, params):
    import torch

    handle = inputs["model"]
    if handle.family in _TEXT_PROMPT_FAMILIES:  # encodes inside its pipeline (LLM text encoders)
        ctx.annotate(prompt=params["prompt"], negative_prompt=params["negative_prompt"])
        return {"conditioning": {"family": handle.family, "prompt": params["prompt"],
                                 "negative_prompt": params["negative_prompt"]}}
    if handle.family == "minimaxh3":  # encodes inside its pipeline; guidance-distilled, no negative prompt
        ctx.annotate(prompt=params["prompt"])
        return {"conditioning": {"family": "minimaxh3", "prompt": params["prompt"]}}
    pipe = handle.pipe
    dev = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    te = pipe.text_encoder
    if handle.memory_mode != "gpu" and not getattr(te, "_sarness_streamed", False):
        dev = torch.device("cpu")  # text encoder could not be streamed; encode on CPU
    ctx.progress(0, 2, "Encoding prompt")
    with torch.inference_mode():
        pe = pipe._get_t5_prompt_embeds(prompt=params["prompt"], max_sequence_length=512, device=dev,
                                        dtype=torch.bfloat16)
        ctx.progress(1, 2, "Encoding negative prompt")
        ne = pipe._get_t5_prompt_embeds(prompt=params["negative_prompt"] or "", max_sequence_length=512,
                                        device=dev, dtype=torch.bfloat16)
    target = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    ctx.progress(2, 2, "Encoded")
    ctx.annotate(prompt=params["prompt"], negative_prompt=params["negative_prompt"])
    return {"conditioning": {"family": handle.family, "prompt_embeds": pe.to(target),
                             "negative_prompt_embeds": ne.to(target)}}


def _denoise_pipe(handle, cls_name: str = "WanPipeline"):
    """A pipeline sharing the loaded components but without the text encoder.

    The text encoder lives in CPU RAM (streamed on demand), so leaving it out keeps
    the pipeline's execution device on the GPU and avoids re-encoding."""
    import diffusers

    pipe = handle.pipe
    cls = getattr(diffusers, cls_name)
    if handle.memory_mode == "gpu" and cls_name == "WanPipeline":
        return pipe
    # Built directly rather than with from_pipe, which casts every shared component to one dtype
    # (undoing FP8 expert storage and the fp32 VAE).
    cache = pipe.__dict__.setdefault("_sarness_denoise", {})
    if cls_name not in cache:
        kwargs = dict(tokenizer=pipe.tokenizer, text_encoder=None, transformer=pipe.transformer,
                      vae=pipe.vae, scheduler=pipe.scheduler)
        if cls_name == "WanImageToVideoPipeline":
            kwargs.update(image_encoder=None, image_processor=None)
        sub = cls(**kwargs)
        if getattr(pipe, "transformer_2", None) is not None:
            sub.register_modules(transformer_2=pipe.transformer_2)
        sub.register_to_config(**{k: v for k, v in pipe.config.items()
                                  if k in ("boundary_ratio", "expand_timesteps")})
        sub.set_progress_bar_config(disable=True)
        cache[cls_name] = sub
    sub = cache[cls_name]
    sub.scheduler = pipe.scheduler
    return sub


@node("sampler.video", title="Video Sampler", category="Sampling", gpu=True,
      description="Generates a clip from the prompt; connect a start frame for image-to-video where supported.",
      inputs=[Socket("model", VIDEO_MODEL), Socket("conditioning", VIDEO_CONDITIONING),
              Socket("start_image", IMAGE, "Start frame", optional=True),
              Socket("end_image", IMAGE, "End frame", optional=True)],
      outputs=[Socket("video", VIDEO)],
      params=[Param("width", "int", 832, min=128, max=1920, step=16, socket=True),
              Param("height", "int", 480, min=128, max=1920, step=16, socket=True),
              Param("frames", "int", 33, min=5, max=241, step=4, socket=True,
                    description="Number of frames; Wan models need 4k+1 frames."),
              Param("fps", "float", 16, min=4, max=60, socket=True),
              Param("seed", "seed", -1, min=-1, max=2**32 - 1, socket=True),
              Param("steps", "int", 30, min=1, max=100, socket=True),
              Param("guidance", "float", 5.0, min=1, max=15, step=0.1, socket=True),
              Param("flow_shift", "float", 3.0, min=0.5, max=12, step=0.1, advanced=True,
                    description="Noise schedule shift; ~3 for 480p, ~5 for 720p.")])
def video_sampler(ctx, inputs, params):
    import diffusers
    import numpy as np
    import torch

    handle = inputs["model"]
    cond = inputs["conditioning"]
    caps = (handle.info.get("capabilities") or {})
    modes = caps.get("modes") or ["t2v"]
    start, end = inputs.get("start_image"), inputs.get("end_image")
    if start is not None and "i2v" not in modes:
        raise WorkflowError(f"{handle.info.get('name')} does not support image-to-video")
    if start is None and "t2v" not in modes:
        raise WorkflowError(f"{handle.info.get('name')} needs a start frame (image-to-video only)")
    if end is not None and not caps.get("end_frame"):
        raise WorkflowError(f"{handle.info.get('name')} does not support end frames")

    limits = handle.info.get("limits") or {}
    mult = int(limits.get("multiple") or 16)
    w, h = int(params["width"]) // mult * mult, int(params["height"]) // mult * mult
    frames = int(params["frames"])
    step = int(limits.get("frame_step") or 4)
    frames = (frames - 1) // step * step + 1
    steps = int(params["steps"])
    seed = int(params["seed"])
    if handle.family == "ltx2":
        return _ltx2_sample(ctx, handle, cond, start, w, h, frames, steps, seed, params)
    if handle.family == "minimaxh3":
        return _minimax_h3_sample(ctx, handle, cond, start, end, w, h, frames, steps, seed)
    if handle.family in ("hunyuanvideo15", "hunyuanvideo15-i2v"):
        return _hunyuan15_sample(ctx, handle, cond, start, w, h, frames, steps, seed, params)

    pipe = handle.pipe
    base_sched = pipe.__dict__.get("_sarness_default_scheduler") or pipe.scheduler
    pipe.scheduler = diffusers.UniPCMultistepScheduler.from_config(base_sched.config, flow_shift=float(params["flow_shift"]))

    def cb(p, i, t, kw):
        ctx.progress(i + 1, steps, f"Step {i + 1}/{steps}")
        return kw

    kwargs: dict[str, Any] = dict(
        prompt_embeds=cond["prompt_embeds"], negative_prompt_embeds=cond["negative_prompt_embeds"],
        height=h, width=w, num_frames=frames, num_inference_steps=steps,
        guidance_scale=float(params["guidance"]),
        generator=torch.Generator(device="cpu").manual_seed(seed),
        output_type="np", callback_on_step_end=cb,
    )
    if start is not None:
        dp = _denoise_pipe(handle, "WanImageToVideoPipeline")
        # Crop-to-fill so the start frame keeps its proportions.
        kwargs["image"] = ImageOps.fit(start[0].convert("RGB"), (w, h), Image.LANCZOS)
        if end is not None:
            kwargs["last_image"] = ImageOps.fit(end[0].convert("RGB"), (w, h), Image.LANCZOS)
    else:
        dp = _denoise_pipe(handle)
    dp.scheduler = pipe.scheduler
    ctx.progress(0, steps, "Sampling")
    with torch.inference_mode():
        out = dp(**kwargs)
    arr = out.frames[0]
    if isinstance(arr, torch.Tensor):
        arr = arr.float().cpu().numpy()
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0, 1) * 255).round().astype(np.uint8)
    pil = [Image.fromarray(f) for f in arr]
    ctx.annotate(seed=seed, steps=steps, guidance=float(params["guidance"]), width=w, height=h,
                 frames=len(pil), fps=float(params["fps"]), flow_shift=float(params["flow_shift"]),
                 mode="i2v" if start is not None else "t2v")
    return {"video": {"frames": pil, "fps": float(params["fps"])}}


def _ltx2_sample(ctx, handle, cond, start, w, h, frames, steps, seed, params):
    """LTX-2 generates the clip and a matching soundtrack in one pass."""
    import diffusers
    import numpy as np
    import torch

    pipe = handle.pipe
    fps = float(params["fps"])
    kwargs: dict[str, Any] = dict(
        prompt=cond["prompt"], negative_prompt=cond.get("negative_prompt") or None, height=h, width=w,
        num_frames=frames, frame_rate=fps, num_inference_steps=steps, guidance_scale=float(params["guidance"]),
        generator=torch.Generator(device="cpu").manual_seed(seed), output_type="np", return_dict=False)

    def cb(p, i, t, kw):
        ctx.progress(i + 1, steps, f"Step {i + 1}/{steps}")
        return kw

    kwargs["callback_on_step_end"] = cb
    if start is not None:
        cache = pipe.__dict__.setdefault("_sarness_denoise", {})
        if "i2v" not in cache:
            cache["i2v"] = diffusers.LTX2ImageToVideoPipeline.from_pipe(pipe, dtype=pipe.transformer.dtype)
            cache["i2v"].set_progress_bar_config(disable=True)
        dp = cache["i2v"]
        kwargs["image"] = ImageOps.fit(start[0].convert("RGB"), (w, h), Image.LANCZOS)
    else:
        dp = pipe
    ctx.progress(0, steps, "Sampling")
    with torch.inference_mode():
        video, audio = dp(**kwargs)
    arr = np.asarray(video[0].float().cpu().numpy() if isinstance(video, torch.Tensor) else video[0])
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0, 1) * 255).round().astype(np.uint8)
    pil = [Image.fromarray(f) for f in arr]
    sound = None
    if audio is not None:
        a = audio[0]
        a = a.float().cpu().numpy() if isinstance(a, torch.Tensor) else np.asarray(a, dtype=np.float32)
        rate = int(getattr(getattr(pipe, "vocoder", None), "config", {}).get("output_sampling_rate", 0) or
                   getattr(pipe, "audio_sampling_rate", 24000))
        sound = {"samples": a, "rate": rate}
    ctx.annotate(seed=seed, steps=steps, guidance=float(params["guidance"]), width=w, height=h, frames=len(pil),
                 fps=fps, audio=sound is not None, mode="i2v" if start is not None else "t2v")
    return {"video": {"frames": pil, "fps": fps, "audio": sound}}


def _frames_to_pil(video):
    import numpy as np
    import torch

    arr = video.float().cpu().numpy() if isinstance(video, torch.Tensor) else np.asarray(video)
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0, 1) * 255).round().astype(np.uint8)
    return [Image.fromarray(f) for f in arr]


def _hunyuan15_sample(ctx, handle, cond, start, w, h, frames, steps, seed, params):
    """HunyuanVideo 1.5: separate text-to-video and image-to-video checkpoints. Guidance lives in the
    pipeline's guider component rather than in the call; the image-to-video pipeline takes its aspect from
    the start frame and its area from target_size (960, i.e. 720p, in the checkpoint), so the frame is cropped to
    the requested size and target_size is set from the requested area."""
    import torch

    pipe = handle.pipe
    guidance = float(params["guidance"])
    pipe.guider.guidance_scale = guidance

    kwargs: dict[str, Any] = dict(prompt=cond["prompt"], negative_prompt=cond.get("negative_prompt") or None,
                                  num_frames=frames, num_inference_steps=steps,
                                  generator=torch.Generator(device="cpu").manual_seed(seed), output_type="np")
    if handle.family == "hunyuanvideo15-i2v":
        kwargs["image"] = ImageOps.fit(start[0].convert("RGB"), (w, h), Image.LANCZOS)
        pipe.target_size = max(16, round((w * h) ** 0.5 / 16) * 16)  # 848×480 → 640, 1280×720 → 960
    else:
        kwargs.update(height=h, width=w)
    # These pipelines take no step callback, so progress and cancellation hook the scheduler's step instead.
    sched = pipe.scheduler
    step_fn = sched.step
    done = [0]

    def step(*a, **kw):
        res = step_fn(*a, **kw)
        done[0] += 1
        ctx.progress(done[0], steps, f"Step {done[0]}/{steps}")
        return res

    ctx.progress(0, steps, "Sampling")
    sched.step = step
    try:
        with torch.inference_mode():
            out = pipe(**kwargs)
    finally:
        del sched.step
    pil = _frames_to_pil(out.frames[0])
    fps = float(params["fps"])
    ctx.annotate(seed=seed, steps=steps, guidance=guidance, width=pil[0].width, height=pil[0].height,
                 frames=len(pil), fps=fps, mode="i2v" if start is not None else "t2v")
    return {"video": {"frames": pil, "fps": fps}}


def _minimax_h3_sample(ctx, handle, cond, start, end, w, h, frames, steps, seed):
    """MiniMax H3: video and a stereo soundtrack in one pass, at a fixed 24 fps. The pipeline snaps the
    frame count up to the next length its VAE decodes (17n + 5, 5-15 s)."""
    import numpy as np
    import torch

    pipe = handle.pipe
    evals = max(steps - 1, 1)  # the step count includes the terminal sigma
    done = [0]

    def on_step(module, args, kwargs):
        done[0] += 1
        ctx.progress(done[0], evals, f"Step {done[0]}/{evals}")

    kwargs: dict[str, Any] = dict(prompt=cond["prompt"], height=h, width=w, num_frames=frames, num_inference_steps=steps,
                                  generator=torch.Generator(device="cpu").manual_seed(seed),
                                  output=["videos", "audio", "sampling_rate"], output_type="np")
    if start is not None:
        kwargs["image"] = ImageOps.fit(start[0].convert("RGB"), (w, h), Image.LANCZOS)
    if end is not None:
        kwargs["last_image"] = ImageOps.fit(end[0].convert("RGB"), (w, h), Image.LANCZOS)
    hook = pipe.transformer.register_forward_pre_hook(on_step, with_kwargs=True)
    ctx.progress(0, evals, "Encoding prompt")
    try:
        with torch.inference_mode():
            out = pipe(**kwargs)
    finally:
        hook.remove()
    video = out["videos"][0]
    arr = np.asarray(video.float().cpu().numpy() if isinstance(video, torch.Tensor) else video)
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0, 1) * 255).round().astype(np.uint8)
    pil = [Image.fromarray(f) for f in arr]
    sound = None
    if out.get("audio") is not None:
        a = out["audio"][0]
        sound = {"samples": a.float().cpu().numpy() if isinstance(a, torch.Tensor) else np.asarray(a, dtype=np.float32),
                 "rate": int(out["sampling_rate"])}
    mode = "fl2v" if start is not None and end is not None else "i2v" if start is not None or end is not None else "t2v"
    ctx.annotate(seed=seed, steps=steps, width=w, height=h, frames=len(pil), fps=24.0, audio=sound is not None, mode=mode)
    return {"video": {"frames": pil, "fps": 24.0, "audio": sound}}


# ----------------------------------------------------------------- frame tools
@node("video.load", title="Load Video", category="Video",
      outputs=[Socket("video", VIDEO), Socket("fps", FLOAT), Socket("frame_count", INT)],
      params=[Param("file", "file", "", file_accept="video"),
              Param("max_frames", "int", 240, min=1, max=5000),
              Param("every_nth", "int", 1, min=1, max=60)])
def load_video(ctx, inputs, params):
    import imageio.v2 as iio

    path = ctx.services.file_path(params["file"])
    if path is None:
        raise WorkflowError("video file is not available on the server")
    reader = iio.get_reader(str(path), format="ffmpeg")
    fps = float(reader.get_meta_data().get("fps") or 24)
    frames = []
    for i, f in enumerate(reader):
        if i % params["every_nth"] == 0:
            frames.append(Image.fromarray(f))
            if len(frames) >= params["max_frames"]:
                break
        ctx.check_cancel()
    reader.close()
    fps = fps / params["every_nth"]
    return {"video": {"frames": frames, "fps": fps}, "fps": fps, "frame_count": len(frames)}


@node("video.to_frames", title="Video To Frames", category="Video",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("frames", IMAGE), Socket("fps", FLOAT)],
      params=[Param("start", "int", 0, min=0), Param("count", "int", 0, min=0, description="0 = all"),
              Param("step", "int", 1, min=1)])
def to_frames(ctx, inputs, params):
    v = inputs["video"]
    fr = v["frames"][params["start"]::params["step"]]
    if params["count"]:
        fr = fr[:params["count"]]
    return {"frames": fr, "fps": v["fps"] / params["step"]}


@node("video.from_frames", title="Frames To Video", category="Video",
      inputs=[Socket("frames", IMAGE)], outputs=[Socket("video", VIDEO)],
      params=[Param("fps", "float", 16, min=1, max=120, socket=True)])
def from_frames(ctx, inputs, params):
    fr = inputs["frames"]
    if not fr:
        raise WorkflowError("no frames")
    size = fr[0].size
    return {"video": {"frames": [f if f.size == size else f.resize(size) for f in fr], "fps": float(params["fps"])}}


@node("video.pick_frame", title="Pick Frame", category="Video",
      description="Extracts one frame (e.g. the last frame to chain clips).",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("image", IMAGE)],
      params=[Param("index", "int", -1, min=-10000, max=10000, description="Negative counts from the end.")])
def pick_frame(ctx, inputs, params):
    fr = inputs["video"]["frames"]
    try:
        return {"image": [fr[params["index"]]]}
    except IndexError:
        raise WorkflowError(f"frame {params['index']} out of range ({len(fr)} frames)")


@node("video.trim", title="Trim Video", category="Video",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("video", VIDEO)],
      params=[Param("start_frame", "int", 0, min=0), Param("end_frame", "int", 0, min=0, description="0 = end")])
def trim(ctx, inputs, params):
    v = inputs["video"]
    end = params["end_frame"] or len(v["frames"])
    return {"video": {**v, "frames": v["frames"][params["start_frame"]:end], "audio": None}}


@node("video.reverse", title="Reverse Video", category="Video",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("video", VIDEO)],
      params=[Param("boomerang", "bool", False, description="Play forward then backward.")])
def reverse(ctx, inputs, params):
    v = inputs["video"]
    fr = list(v["frames"])
    out = fr + fr[-2:0:-1] if params["boomerang"] else fr[::-1]
    return {"video": {**v, "frames": out, "audio": None}}


@node("video.concat", title="Concatenate Videos", category="Video",
      inputs=[Socket("a", VIDEO), Socket("b", VIDEO)], outputs=[Socket("video", VIDEO)])
def concat(ctx, inputs, params):
    a, b = inputs["a"], inputs["b"]
    size = a["frames"][0].size
    return {"video": {"frames": a["frames"] + [f.resize(size) if f.size != size else f for f in b["frames"]],
                      "fps": a["fps"]}}


@node("video.resize", title="Resize Video", category="Video",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("video", VIDEO)],
      params=[Param("width", "int", 1280, min=16, max=4096), Param("height", "int", 720, min=16, max=4096)])
def resize_video(ctx, inputs, params):
    v = inputs["video"]
    size = (params["width"] // 2 * 2, params["height"] // 2 * 2)
    return {"video": {**v, "frames": [f.resize(size, Image.LANCZOS) for f in v["frames"]]}}


@node("video.interpolate", title="Interpolate Frames", category="Video",
      description="Doubles the frame rate by blending neighbouring frames (fast, not motion-compensated).",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("video", VIDEO)],
      params=[Param("passes", "int", 1, min=1, max=3)])
def interpolate(ctx, inputs, params):
    v = inputs["video"]
    fr = v["frames"]
    for _ in range(params["passes"]):
        out = []
        for a, b in zip(fr, fr[1:]):
            out.extend([a, Image.blend(a, b, 0.5)])
        out.append(fr[-1])
        fr = out
    return {"video": {"frames": fr, "fps": v["fps"] * (2 ** params["passes"])}}


@node("video.set_fps", title="Set Frame Rate", category="Video",
      inputs=[Socket("video", VIDEO)], outputs=[Socket("video", VIDEO)],
      params=[Param("fps", "float", 24, min=1, max=120, socket=True)])
def set_fps(ctx, inputs, params):
    return {"video": {**inputs["video"], "fps": float(params["fps"]), "audio": None}}
