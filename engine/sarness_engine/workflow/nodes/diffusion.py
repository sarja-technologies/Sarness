"""Image diffusion nodes backed by Hugging Face Diffusers.

SDXL / SD 1.5 run step by step through encode → sample → decode. Newer families
(Qwen-Image 2.1) run their diffusers pipeline end to end: the prompt node passes the
text through, the sampler returns finished images and VAE Decode passes them on."""

from __future__ import annotations

import base64
import io
from typing import Any

from ..registry import node
from ..styles import apply_style, style_options
from ..types import (CONDITIONING, CONTROL, CONTROLNET, FLOAT, IMAGE, INT, LATENT, MASK, MODEL, SCHEDULER, STRING,
                     VAE, Param, Socket, WorkflowError)

IMAGE_FAMILIES = ["sdxl", "sd15", "qwenimage21", "qwenimage", "qwenimage-edit", "zimage", "flux2klein", "flux2",
                  "gateway"]
# Families whose diffusers pipeline runs end to end, and the pipeline class for each mode (None: the loaded one).
# "edit" passes the reference image(s) as conditioning; img2img starts from the reference and keeps its layout.
NATIVE_MODES: dict[str, dict[str, str | None]] = {
    "qwenimage21": {"txt2img": None},
    "qwenimage": {"txt2img": None, "img2img": "QwenImageImg2ImgPipeline", "inpaint": "QwenImageInpaintPipeline"},
    "qwenimage-edit": {"edit": None},
    "zimage": {"txt2img": None, "img2img": "ZImageImg2ImgPipeline", "inpaint": "ZImageInpaintPipeline"},
    "flux2klein": {"txt2img": None, "edit": None, "inpaint": "Flux2KleinInpaintPipeline"},
    "flux2": {"txt2img": None, "edit": None},
}
NATIVE_IMAGE_FAMILIES = set(NATIVE_MODES)
# How each native family takes guidance: Qwen-Image runs true CFG with a negative prompt, Z-Image classic CFG
# (with a negative prompt), FLUX.2 an embedded guidance scale and no negative prompt.
_TRUE_CFG = {"qwenimage21", "qwenimage", "qwenimage-edit"}
_NO_NEGATIVE = {"flux2klein", "flux2"}
MEMORY_MODES = [
    {"value": "auto", "label": "Auto"},
    {"value": "gpu", "label": "GPU (fastest)"},
    {"value": "offload", "label": "Offload (lower VRAM)"},
    {"value": "sequential", "label": "Sequential (minimum VRAM, slow)"},
]
SCHEDULER_OPTIONS = ["default", "euler_a", "euler", "dpmpp_2m", "dpmpp_2m_karras", "dpmpp_sde_karras",
                     "ddim", "unipc", "lcm", "heun", "turbo"]

# Linear latent→RGB projection for cheap SDXL previews.
_SDXL_RGB = [[0.3651, 0.4232, 0.4341], [-0.2533, -0.0042, 0.1068], [0.1076, 0.1111, -0.0362], [-0.3165, -0.2492, -0.2188]]
_SDXL_BIAS = [0.1084, -0.0175, -0.0011]


def _model(ctx, model_id: str) -> dict[str, Any]:
    info = ctx.services.model_info(model_id)
    if info is None:
        raise WorkflowError(f"model '{model_id}' is not installed on the server")
    return info


# --------------------------------------------------------------------- loaders
@node("loader.image_model", title="Load Image Model", category="Loaders", gpu=True,
      description="Loads a diffusion checkpoint (Diffusers folder or single .safetensors).",
      outputs=[Socket("model", MODEL)],
      params=[Param("model", "model", "", model_category="image", model_family=IMAGE_FAMILIES),
              Param("vae", "model", "", model_category="vae", optional=True, advanced=True,
                    description="Optional VAE override, e.g. the SDXL fp16-fix VAE."),
              Param("memory_mode", "enum", "auto", options=MEMORY_MODES, advanced=True)])
def load_image_model(ctx, inputs, params):
    info = _model(ctx, params["model"])
    ctx.progress(0, 1, "Loading model")
    handle = ctx.services.models.image_pipeline(info, params["memory_mode"])
    if params.get("vae"):
        handle.vae = ctx.services.models.vae(_model(ctx, params["vae"]))
    ctx.annotate(model_id=info["id"], model_name=info.get("name"), memory_mode=handle.memory_mode,
                 vae=params.get("vae") or None)
    return {"model": handle}


@node("loader.lora", title="Apply LoRA", category="Loaders",
      description="Adds a LoRA adapter to the model with the given strength.",
      inputs=[Socket("model", MODEL)], outputs=[Socket("model", MODEL)],
      params=[Param("lora", "model", "", model_category="lora"),
              Param("strength", "float", 1.0, min=-4, max=4, step=0.05, socket=True)])
def apply_lora(ctx, inputs, params):
    from ...runtime.models import LoraRef

    info = _model(ctx, params["lora"])
    handle = inputs["model"]
    ref = LoraRef(info["id"], info["path"], float(params["strength"]))
    loras = list(ctx.lineage.get("loras") or [])
    ctx.annotate(loras=loras + [{"id": info["id"], "name": info.get("name"), "strength": ref.weight}])
    return {"model": handle.with_lora(ref)}


@node("loader.vae", title="Load VAE", category="Loaders", gpu=True,
      outputs=[Socket("vae", VAE)], params=[Param("vae", "model", "", model_category="vae")])
def load_vae(ctx, inputs, params):
    return {"vae": ctx.services.models.vae(_model(ctx, params["vae"]))}


@node("loader.controlnet", title="Load ControlNet", category="Loaders",
      outputs=[Socket("controlnet", CONTROLNET)],
      params=[Param("controlnet", "model", "", model_category="controlnet")])
def load_controlnet(ctx, inputs, params):
    info = _model(ctx, params["controlnet"])
    ctx.annotate(controlnet=info["id"])
    return {"controlnet": {"model": ctx.services.models.controlnet(info), "id": info["id"]}}


# ---------------------------------------------------------------------- prompt
@node("prompt.encode", title="Encode Prompt", category="Prompt", gpu=True,
      description="Encodes positive and negative prompts with the model's text encoders.",
      inputs=[Socket("model", MODEL)], outputs=[Socket("conditioning", CONDITIONING)],
      params=[Param("prompt", "text", "", socket=True),
              Param("negative_prompt", "text", "", socket=True),
              Param("style", "enum", "none", options=style_options(),
                    description="Wraps the prompt with a reusable look, e.g. an ordinary iPhone photo."),
              Param("clip_skip", "int", 0, min=0, max=4, advanced=True)])
def encode_prompt(ctx, inputs, params):
    import torch

    handle = inputs["model"]
    pipe = handle.pipe
    style = params.get("style") or "none"
    prompt, negative = apply_style(style, params["prompt"], params["negative_prompt"] or "")
    ctx.annotate(prompt=params["prompt"], negative_prompt=params["negative_prompt"],
                 style=style if style != "none" else None)
    if handle.family in NATIVE_IMAGE_FAMILIES:
        return {"conditioning": {"family": handle.family, "prompt": prompt, "negative_prompt": negative}}
    from ...runtime.models import apply_loras, device

    apply_loras(handle)  # LoRAs may patch the text encoders
    dev = device()
    with torch.inference_mode():
        if handle.family == "sdxl":
            pe, ne, pp, npool = pipe.encode_prompt(
                prompt=prompt, prompt_2=None, device=dev, num_images_per_prompt=1,
                do_classifier_free_guidance=True, negative_prompt=negative or None,
                clip_skip=params["clip_skip"] or None)
            cond = {"family": "sdxl", "prompt_embeds": pe, "negative_prompt_embeds": ne,
                    "pooled_prompt_embeds": pp, "negative_pooled_prompt_embeds": npool}
        else:
            pe, ne = pipe.encode_prompt(prompt, dev, 1, True, negative or None,
                                        clip_skip=params["clip_skip"] or None)
            cond = {"family": handle.family, "prompt_embeds": pe, "negative_prompt_embeds": ne}
    return {"conditioning": cond}


# ---------------------------------------------------------------------- latent
@node("latent.empty", title="Empty Latent", category="Latent",
      description="Starting noise for text-to-image at the given size.",
      outputs=[Socket("latent", LATENT)],
      params=[Param("width", "int", 1024, min=64, max=4096, step=8, socket=True),
              Param("height", "int", 1024, min=64, max=4096, step=8, socket=True),
              Param("batch_size", "int", 1, min=1, max=8, socket=True)])
def empty_latent(ctx, inputs, params):
    w, h = int(params["width"]) // 8 * 8, int(params["height"]) // 8 * 8
    ctx.annotate(width=w, height=h, batch_size=params["batch_size"])
    return {"latent": {"samples": None, "width": w, "height": h, "batch": int(params["batch_size"])}}


@node("latent.upscale", title="Upscale Latent", category="Latent",
      description="Resizes latents for a second, refining pass (hi-res fix).",
      inputs=[Socket("latent", LATENT)], outputs=[Socket("latent", LATENT)],
      params=[Param("factor", "float", 1.5, min=1.0, max=4.0, step=0.05),
              Param("mode", "enum", "bicubic", options=["nearest", "bilinear", "bicubic"])])
def upscale_latent(ctx, inputs, params):
    import torch.nn.functional as F

    lat = inputs["latent"]
    if lat.get("samples") is None:
        raise WorkflowError("Upscale Latent needs sampled latents (connect it after a sampler)")
    s = lat["samples"]
    h, w = int(s.shape[-2] * params["factor"]), int(s.shape[-1] * params["factor"])
    kwargs = {} if params["mode"] == "nearest" else {"align_corners": False}
    out = F.interpolate(s.float(), size=(h, w), mode=params["mode"], **kwargs).to(s.dtype)
    return {"latent": {**lat, "samples": out, "width": w * 8, "height": h * 8, "source": None}}


# ------------------------------------------------------------------ schedulers
@node("scheduler.select", title="Scheduler", category="Schedulers",
      description="Overrides the sampler's noise schedule.",
      outputs=[Socket("scheduler", SCHEDULER)],
      params=[Param("name", "enum", "dpmpp_2m_karras", options=SCHEDULER_OPTIONS),
              Param("timestep_spacing", "enum", "default", options=["default", "leading", "trailing", "linspace"], advanced=True)])
def scheduler_select(ctx, inputs, params):
    opts = {} if params["timestep_spacing"] == "default" else {"timestep_spacing": params["timestep_spacing"]}
    ctx.annotate(scheduler=params["name"])
    return {"scheduler": {"name": params["name"], "options": opts}}


# --------------------------------------------------------------------- control
@node("control.apply", title="Apply ControlNet", category="Conditioning",
      inputs=[Socket("controlnet", CONTROLNET), Socket("image", IMAGE, "Hint image")],
      outputs=[Socket("control", CONTROL)],
      params=[Param("strength", "float", 0.7, min=0, max=2, step=0.05, socket=True),
              Param("start", "float", 0.0, min=0, max=1, step=0.01, advanced=True),
              Param("end", "float", 1.0, min=0, max=1, step=0.01, advanced=True)])
def apply_control(ctx, inputs, params):
    ctx.annotate(control_strength=params["strength"])
    return {"control": {**inputs["controlnet"], "image": inputs["image"], "strength": params["strength"],
                        "start": params["start"], "end": params["end"]}}


# --------------------------------------------------------------------- sampler
def _preview_cb(ctx, total_steps: int, family: str):
    import torch

    rgb = torch.tensor(_SDXL_RGB)
    bias = torch.tensor(_SDXL_BIAS)

    def cb(pipe, step, timestep, kwargs):
        preview = None
        lat = kwargs.get("latents")
        if family == "sdxl" and lat is not None and (step % 2 == 0 or step == total_steps - 1):
            try:
                preview = _latent_preview(lat[0], rgb, bias)
            except Exception:
                preview = None
        ctx.progress(step + 1, total_steps, f"Step {step + 1}/{total_steps}", preview)
        return kwargs

    return cb


def _latent_preview(lat, rgb, bias) -> str:
    import torch
    from PIL import Image

    with torch.no_grad():
        x = torch.einsum("chw,cr->hwr", lat.float().cpu(), rgb) + bias
        x = ((x + 1) / 2).clamp(0, 1).mul(255).byte().numpy()
    im = Image.fromarray(x)
    im = im.resize((im.width * 4, im.height * 4), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@node("sampler.image", title="Image Sampler", category="Sampling", gpu=True, cacheable=True,
      description="Denoises latents with the model. Connect a mask for inpainting, or encoded image latents for image-to-image.",
      inputs=[Socket("model", MODEL), Socket("conditioning", CONDITIONING), Socket("latent", LATENT),
              Socket("scheduler", SCHEDULER, optional=True), Socket("control", CONTROL, optional=True),
              Socket("mask", MASK, "Inpaint mask", optional=True),
              Socket("reference_2", IMAGE, "Extra reference", optional=True),
              Socket("reference_3", IMAGE, "Extra reference", optional=True)],
      outputs=[Socket("latent", LATENT)],
      params=[Param("seed", "seed", -1, min=-1, max=2**32 - 1, socket=True),
              Param("steps", "int", 30, min=1, max=150, socket=True),
              Param("guidance", "float", 6.0, min=0, max=30, step=0.1, socket=True, label="Guidance (CFG)"),
              Param("denoise", "float", 1.0, min=0.01, max=1.0, step=0.01, socket=True,
                    description="Strength for image-to-image / inpainting (1.0 = ignore the input)."),
              Param("guidance_rescale", "float", 0.0, min=0, max=1, step=0.05, advanced=True)])
def image_sampler(ctx, inputs, params):
    import torch

    from ...runtime.models import apply_loras, apply_scheduler

    handle = inputs["model"]
    cond = inputs["conditioning"]
    lat = inputs["latent"]
    control = inputs.get("control")
    mask = inputs.get("mask")
    if cond.get("family") != handle.family:
        raise WorkflowError("conditioning was encoded with a different model family")
    refs = [im for k in ("reference_2", "reference_3") for im in (inputs.get(k) or [])]
    if handle.family in NATIVE_IMAGE_FAMILIES:
        return _native_sample(ctx, handle, cond, lat, control, mask, refs, params)
    if refs:
        raise WorkflowError(f"{handle.info.get('name') or handle.model_id} does not take extra reference images")

    base = handle.pipe
    apply_loras(handle)
    apply_scheduler(base, inputs.get("scheduler"))

    steps, guidance, denoise = int(params["steps"]), float(params["guidance"]), float(params["denoise"])
    seed = int(params["seed"])
    batch = int(lat.get("batch") or 1)
    gen = torch.Generator(device="cpu").manual_seed(seed)

    kwargs: dict[str, Any] = {
        "num_inference_steps": steps, "guidance_scale": guidance, "generator": gen, "output_type": "latent",
        "prompt_embeds": cond["prompt_embeds"], "negative_prompt_embeds": cond["negative_prompt_embeds"],
    }
    if handle.family == "sdxl":
        kwargs["pooled_prompt_embeds"] = cond["pooled_prompt_embeds"]
        kwargs["negative_pooled_prompt_embeds"] = cond["negative_pooled_prompt_embeds"]
    if params["guidance_rescale"]:
        kwargs["guidance_rescale"] = float(params["guidance_rescale"])

    mode = "txt2img"
    if mask is not None:
        mode = "inpaint"
    elif lat.get("samples") is not None:
        mode = "img2img"
    pipe = _variant_pipe(base, handle.family, mode, control)

    if mode == "txt2img":
        kwargs.update(width=lat["width"], height=lat["height"], num_images_per_prompt=batch)
        effective_steps = steps
    elif mode == "img2img":
        kwargs.update(image=lat["samples"], strength=denoise)
        effective_steps = max(1, int(steps * denoise))
    else:
        src = lat.get("source")
        if not src:
            raise WorkflowError("inpainting needs latents from 'VAE Encode' of the source image")
        m = mask[0].resize(src[0].size)
        kwargs.update(image=src, mask_image=[m] * len(src), strength=denoise,
                      width=src[0].width, height=src[0].height)
        effective_steps = max(1, int(steps * denoise))

    if control is not None:
        hint = [im.resize((lat["width"], lat["height"])) for im in control["image"]]
        key = "control_image" if mode != "txt2img" else "image"
        kwargs[key] = hint[0] if len(hint) == 1 else hint
        kwargs["controlnet_conditioning_scale"] = float(control["strength"])
        kwargs["control_guidance_start"] = float(control["start"])
        kwargs["control_guidance_end"] = float(control["end"])

    kwargs["callback_on_step_end"] = _preview_cb(ctx, effective_steps, handle.family)
    with torch.inference_mode():
        out = pipe(**kwargs)
    samples = out.images
    if torch.isnan(samples).any():
        raise WorkflowError("sampling produced NaNs (try another scheduler, lower guidance, or the fp16-fix VAE)")
    ctx.annotate(seed=seed, steps=steps, guidance=guidance, denoise=denoise if mode != "txt2img" else None,
                 mode=mode, scheduler=(inputs.get("scheduler") or {}).get("name", "default"))
    h, w = samples.shape[-2] * 8, samples.shape[-1] * 8
    return {"latent": {"samples": samples, "width": w, "height": h, "batch": samples.shape[0], "source": lat.get("source")}}


def _native_pipe(handle, cls_name: str | None):
    """The loaded pipeline, or a task variant (image-to-image, inpainting) sharing its components."""
    import diffusers

    base = handle.pipe
    if cls_name is None:
        return base
    cache = base.__dict__.setdefault("_sarness_denoise", {})
    if cls_name not in cache:
        sub = getattr(diffusers, cls_name).from_pipe(base)
        sub.set_progress_bar_config(disable=True)
        cache[cls_name] = sub
    return cache[cls_name]


def _native_sample(ctx, handle, cond, lat, control, mask, refs, params):
    import torch

    from ...runtime.models import apply_loras

    name = handle.info.get("name") or handle.model_id
    family = handle.family
    table = NATIVE_MODES[family]
    if control is not None:
        raise WorkflowError(f"{name} does not support ControlNet in Sarness")
    if lat.get("samples") is not None:
        raise WorkflowError(f"{name} starts from images, not latents: connect VAE Encode directly to the sampler")
    source = lat.get("source")
    if mask is not None:
        if not source:
            raise WorkflowError("inpainting needs the source image (connect it through VAE Encode)")
        mode = "inpaint"
    elif source:
        mode = "edit" if "edit" in table else "img2img"
    else:
        mode = "txt2img"
    if mode not in table:
        what = {"img2img": "image-to-image", "inpaint": "inpainting", "edit": "reference editing",
                "txt2img": "text-to-image without a reference image"}[mode]
        raise WorkflowError(f"{name} does not support {what}")
    if refs and mode not in ("edit", "inpaint") and "edit" not in table:
        raise WorkflowError(f"{name} does not take extra reference images")

    mult = int((handle.info.get("limits") or {}).get("multiple") or 16)
    w, h = max(mult, lat["width"] // mult * mult), max(mult, lat["height"] // mult * mult)
    steps, guidance, seed = int(params["steps"]), float(params["guidance"]), int(params["seed"])
    denoise = float(params["denoise"])
    batch = int(lat.get("batch") or 1)

    def cb(pipe, step, timestep, kwargs):
        ctx.progress(step + 1, total, f"Step {step + 1}/{total}")
        return kwargs

    kwargs: dict[str, Any] = dict(prompt=cond["prompt"], height=h, width=w, num_inference_steps=steps,
                                  generator=torch.Generator(device="cpu").manual_seed(seed), output_type="pil",
                                  callback_on_step_end=cb)
    negative = cond.get("negative_prompt") or ""
    if family in _TRUE_CFG:
        if guidance > 1:
            # True CFG only engages with a negative prompt; a blank one is the model card's default.
            kwargs.update(true_cfg_scale=guidance, negative_prompt=negative or " ")
    else:
        kwargs["guidance_scale"] = guidance
        if family not in _NO_NEGATIVE and guidance > 1 and negative:
            kwargs["negative_prompt"] = negative

    total = steps
    if mode == "img2img":
        kwargs.update(image=source[0], strength=denoise)
        total = max(1, int(steps * denoise))
    elif mode == "inpaint":
        kwargs.update(image=source[0], mask_image=mask[0].resize(source[0].size), strength=denoise)
        if refs:
            kwargs["image_reference"] = refs
        total = max(1, int(steps * denoise))
    elif mode == "edit":
        images = [*source, *refs]
        limit = int((handle.info.get("capabilities") or {}).get("references") or len(images))
        kwargs["image"] = images[:limit]
    elif refs:
        kwargs["image"] = refs  # FLUX.2: references guide a new image
    if mode in ("txt2img", "edit"):
        kwargs["num_images_per_prompt"] = batch
    pipe = _native_pipe(handle, table[mode])
    apply_loras(handle)
    ctx.progress(0, total, "Sampling")
    with torch.inference_mode():
        images = pipe(**kwargs).images
    ctx.annotate(seed=seed, steps=steps, guidance=guidance, mode=mode, width=w, height=h,
                 denoise=denoise if mode in ("img2img", "inpaint") else None,
                 references=(len(kwargs.get("image") or []) if mode == "edit" else len(refs)) or None)
    return {"latent": {"samples": None, "images": images, "width": images[0].width, "height": images[0].height,
                       "batch": len(images), "source": None}}


def _variant_pipe(base, family: str, mode: str, control):
    """Builds a task-specific pipeline that shares the loaded components."""
    import diffusers as d

    if family == "sdxl":
        table = {
            ("txt2img", False): None,
            ("img2img", False): d.StableDiffusionXLImg2ImgPipeline,
            ("inpaint", False): d.StableDiffusionXLInpaintPipeline,
            ("txt2img", True): d.StableDiffusionXLControlNetPipeline,
            ("img2img", True): d.StableDiffusionXLControlNetImg2ImgPipeline,
            ("inpaint", True): d.StableDiffusionXLControlNetInpaintPipeline,
        }
    else:
        table = {
            ("txt2img", False): None,
            ("img2img", False): d.StableDiffusionImg2ImgPipeline,
            ("inpaint", False): d.StableDiffusionInpaintPipeline,
            ("txt2img", True): d.StableDiffusionControlNetPipeline,
            ("img2img", True): d.StableDiffusionControlNetImg2ImgPipeline,
            ("inpaint", True): d.StableDiffusionControlNetInpaintPipeline,
        }
    cls = table[(mode, control is not None)]
    if cls is None:
        return base
    extra = {"controlnet": control["model"]} if control is not None else {}
    if control is not None:
        import torch

        cn = control["model"]
        dev = base._execution_device
        if next(cn.parameters()).device != dev:
            cn.to(dev, dtype=torch.float16 if dev.type == "cuda" else torch.float32)
    # from_pipe() casts to float32 unless told otherwise, which would silently
    # convert the shared, already-loaded fp16 UNet (2x VRAM, ~20x slower).
    pipe = cls.from_pipe(base, dtype=base.unet.dtype, **extra)
    pipe.set_progress_bar_config(disable=True)
    return pipe


# ------------------------------------------------------------------------- VAE
def _vae_for(handle, override=None):
    return override or handle.vae or handle.pipe.vae


@node("vae.decode", title="VAE Decode", category="VAE", gpu=True,
      description="Turns latents into images.",
      inputs=[Socket("model", MODEL), Socket("latent", LATENT), Socket("vae", VAE, "VAE override", optional=True)],
      outputs=[Socket("image", IMAGE)],
      params=[Param("tiled", "bool", False, advanced=True, description="Decode in tiles to save VRAM at large sizes.")])
def vae_decode(ctx, inputs, params):
    import torch

    from ...runtime.models import device

    handle = inputs["model"]
    lat = inputs["latent"]
    if lat.get("images") is not None:  # end-to-end pipelines return finished images
        return {"image": lat["images"]}
    if lat.get("samples") is None:
        raise WorkflowError("nothing to decode: connect latents from a sampler or VAE Encode")
    vae = _vae_for(handle, inputs.get("vae"))
    pipe = handle.pipe
    samples = lat["samples"]
    if params["tiled"]:
        vae.enable_tiling()
    else:
        vae.disable_tiling()
    upcast = vae.dtype == torch.float16 and getattr(vae.config, "force_upcast", False)
    ctx.progress(0, 1, "Decoding")
    with torch.inference_mode():
        if upcast:
            vae.to(dtype=torch.float32)
        # Offloaded VAEs are moved by diffusers' forward hooks on encode/decode.
        x = samples.to(device=device(), dtype=vae.dtype) / vae.config.scaling_factor
        decoded = []
        for i in range(x.shape[0]):  # decode one at a time to bound peak VRAM
            decoded.append(vae.decode(x[i:i + 1], return_dict=False)[0])
        image = torch.cat(decoded)
        if upcast:
            vae.to(dtype=torch.float16)
    if torch.isnan(image).any():
        raise WorkflowError("VAE produced NaNs; enable the SDXL fp16-fix VAE in the model loader")
    images = pipe.image_processor.postprocess(image.float(), output_type="pil")
    ctx.progress(1, 1, "Decoded")
    return {"image": images}


@node("vae.encode", title="VAE Encode", category="VAE", gpu=True,
      description="Turns images into latents for image-to-image or inpainting.",
      inputs=[Socket("model", MODEL), Socket("image", IMAGE), Socket("vae", VAE, "VAE override", optional=True)],
      outputs=[Socket("latent", LATENT)],
      params=[Param("sample", "bool", False, advanced=True,
                    description="Sample the latent distribution instead of using its mean."),
              Param("seed", "int", 0, min=0, max=2**32 - 1, advanced=True)])
def vae_encode(ctx, inputs, params):
    import torch

    from ...runtime.models import device

    handle = inputs["model"]
    if handle.family in NATIVE_IMAGE_FAMILIES:
        # End-to-end pipelines encode the image themselves; pass it through for the sampler.
        images = [im.convert("RGB") for im in inputs["image"]]
        w, h = images[0].size
        return {"latent": {"samples": None, "width": w, "height": h, "batch": len(images), "source": images}}
    vae = _vae_for(handle, inputs.get("vae"))
    pipe = handle.pipe
    images = [im.resize((im.width // 8 * 8, im.height // 8 * 8)) for im in inputs["image"]]
    upcast = vae.dtype == torch.float16 and getattr(vae.config, "force_upcast", False)
    gen = torch.Generator(device="cpu").manual_seed(int(params["seed"]))
    with torch.inference_mode():
        if upcast:
            vae.to(dtype=torch.float32)
        x = pipe.image_processor.preprocess(images).to(device=device(), dtype=vae.dtype)
        latents = []
        for i in range(x.shape[0]):
            dist = vae.encode(x[i:i + 1]).latent_dist
            latents.append(dist.sample(generator=gen) if params["sample"] else dist.mode())
        z = torch.cat(latents) * vae.config.scaling_factor
        if upcast:
            vae.to(dtype=torch.float16)
            z = z.to(torch.float16)
    w, h = images[0].size
    return {"latent": {"samples": z, "width": w, "height": h, "batch": len(images), "source": images}}
