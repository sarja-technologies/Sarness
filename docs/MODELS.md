# Models: what has been tested

Measurements were taken on the development machine (RTX 3060 12 GB, i9-11900F,
32 GB RAM, Ubuntu 26.04, driver 595, PyTorch 2.11 cu128, diffusers 0.41,
llama.cpp b11606 CUDA 12.8) through the public engine API. The target server's
RTX 4070 Ti has the same 12 GB VRAM and should be noticeably faster.
All models were removed from the development machine afterwards; install them
on the AI server from **Models → Catalog**.

## Verified end to end (real inference)

| Model | What was tested | Result |
|---|---|---|
| **Qwen3 4B Instruct 2507 Q4_K_M** (GGUF) | Streaming chat, regenerate/branching, Markdown tables, desktop UI | ~99 tok/s, first token ~1 s incl. load, ~3.9 GB VRAM |
| **Gemma 4 E4B Instruct Q4_0 + mmproj** (GGUF, vision) | Image attachment → correct description; thinking on/off | ~72 tok/s; 0.5 s answer with thinking off, ~9 s with thinking |
| **SDXL Turbo** (Diffusers fp16) | txt2img 512² 4 steps, batch of 2, img2img, inpaint (mask), desktop UI generate, advanced graph run, mid-run cancel | 0.8–1.4 s warm, 15 s cold (7 s load); peak 7.7 GB VRAM |
| SDXL Turbo + **LCM LoRA** + **SDXL fp16-fix VAE** | 1024², 6 steps, LCM scheduler | 4.7 s, peak 9.6 GB |
| SDXL Turbo + **Real-ESRGAN x4plus** | Upscale result to 2× | 1.4 s total |
| SDXL Turbo, custom graph | Hi-res fix (sample → upscale latent → second sampler) 768² | 1.3 s; correctly reported as not representable in simple mode |
| **Wan 2.1 T2V 1.3B** (Diffusers) | Text-to-video 832×480, 33 frames, 30 steps, "split" placement | 272 s incl. 29 s load, peak 5.2 GB VRAM, coherent motion |

## Verified on the AI server through its gateway (4070ri-aiserver, RTX 4070 Ti 12 GB)

Run through the Sarness engine on the server, which forwards jobs to the existing AI gateway
(see DEPLOYMENT.md §3b). Times are wall clock as seen by Sarness, including a cold model load.

| Model | Test | Result |
|---|---|---|
| Z-Image Turbo (`gw:z-image-turbo`) | txt2img 1024², 9 steps | 77 s cold (≈17 s generation), correct image in library |
| Wan 2.2 TI2V 5B (`gw:wan2.2-ti2v-5b`) | t2v 832×480, 33 frames, 20 steps | 379 s, 1.4 s clip at 24 fps, thumbnail OK |
| Qwen3.5 9B via gateway `/v1` (Ollama) | streaming chat, thinking off / on | ≈77 tok/s both; thinking off uses `reasoning_effort: "none"` |
| Qwen-Image 2.1 Turbo (`gw:qwen-image-2.1-turbo`) | — | Listed and wired (text-to-image + reference edit); not run through Sarness yet |

## Experiments: God's Paintbrush (server RTX 4070 Ti)

These models are used only by the God's Paintbrush experiment and are installed from its own model panel, not
from the Catalog. Provenance, licences, candidate comparison and full measurements are in
[EXPERIMENTS.md](EXPERIMENTS.md).

| Model | Use | Result |
|---|---|---|
| GauGAN/SPADE Labels2Landscapes 512 (community, **CC BY-NC-SA 4.0**) | Live preview from the painted label map | 72 ms wall / 62 ms GPU at 768×512, 1.1 GB peak; cold start 3.5 s |
| SD 1.5 + ControlNet 1.1 Seg (fp16, OpenRAIL-M) | Photographic refine (img2img over the live frame) | 1.36 s at 768×512 with 20 steps, 3.9 GB peak; 8.3 s at 1536×1024 |

## Installed but not exercised

| Model | Status |
|---|---|
| SDXL 1.0 base | Same code path as SDXL Turbo (verified); 1024² run not completed. Expect ~9–10 GB VRAM. |
| Wan 2.2 TI2V 5B | **Untested.** Text- and image-to-video. On 12 GB it needs streamed ("offload") placement and ≥ 32 GB system RAM; expect several minutes per short clip. |
| Gemma 4 12B, Qwen3.5 0.8B | Not run; standard GGUF via llama.cpp. |
| ControlNet Canny SDXL | Node path implemented, not run. |

## VRAM / RAM notes for a 12 GB GPU

* One large diffusion model is resident at a time; the GPU worker exits after
  the idle timeout and returns all VRAM.
* An idle chat model is unloaded automatically when a generation needs its VRAM;
  loading a chat model stops an idle diffusion worker first. Chat models use
  llama.cpp `--fit` so they partially offload instead of crashing.
* Wan's 11 GB UMT5 text encoder is kept in **system RAM** and streamed to the GPU.
  Video models therefore need lots of RAM: ≥ 24 GB for Wan 2.1 1.3B, ≥ 32 GB
  (with nothing else heavy running) for Wan 2.2 5B. Do not run them on a desktop
  that is also used for other work.
* On a CUDA out-of-memory error the worker releases everything and the job is
  retried once with offloading.

## Not supported (honest list)

FLUX, SD3, LTX-Video, HunyuanVideo, CogVideoX, Qwen-Image and Z-Image pipelines
are recognised but have no runtime adapter yet; the model browser flags them
before download. IP-Adapter, prompt weighting syntax and motion-control
parameters are not implemented.
