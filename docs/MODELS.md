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

Re-measured on 2026-10-21 after the multi-server engine was deployed, through the desktop proxy with both servers
connected: Z-Image Turbo 1024² took 177 s (first run, while a chat model streamed on the Spark) and 100 s (second
run). The engine's gateway adapter is unchanged on this branch; the time is spent inside the gateway.

## Experiments: God's Paintbrush (server RTX 4070 Ti)

These models are used only by the God's Paintbrush experiment and are installed from its own model panel, not
from the Catalog. Provenance, licences, candidate comparison and full measurements are in
[EXPERIMENTS.md](EXPERIMENTS.md).

| Model | Use | Result |
|---|---|---|
| GauGAN/SPADE Labels2Landscapes 512 (community, **CC BY-NC-SA 4.0**) | Live preview from the painted label map | 72 ms wall / 62 ms GPU at 768×512, 1.1 GB peak; cold start 3.5 s |
| SD 1.5 + ControlNet 1.1 Seg (fp16, OpenRAIL-M) | Photographic refine (img2img over the live frame) | 1.36 s at 768×512 with 20 steps, 3.9 GB peak; 8.3 s at 1536×1024 |

Re-checked on 2026-10-21 after the multi-server upgrade, through the desktop proxy: live 347 ms including a cold
load (67 ms warm), refine 1.50 s at 768×512, peaks 1.1 GB and 3.9 GB.

## Verified on the NVIDIA DGX Spark (GB10, 128 GB unified memory, ARM64)

Engine 0.4.0 on the Spark itself (see MULTI-SERVER.md §3): PyTorch 2.14.1+cu130, diffusers 0.41,
llama.cpp v0.7.0 built for sm_121. Loads read weights without mmap (`--load-mode dio` for llama.cpp).

**Chat models** (llama.cpp, context 32 768, a ~2 200-token prompt and 256 generated tokens, measured with
llama-server's own timings; memory is the drop in available system memory):

| Model | Load | Prompt | Generation | Memory |
|---|---|---|---|---|
| Qwen3.5 0.8B Q8_0 | 2.4 s | 13 848 tok/s | 204 tok/s | 2.2 GB |
| Qwen3 4B Instruct 2507 Q4_K_M | 2.1 s | 4 963 tok/s | 69.1 tok/s | 7.8 GB |
| Qwen3.6 35B-A3B MXFP4 (vision) | 5.2 s | 2 087 tok/s | 66.8 tok/s | 23.4 GB |
| Qwen3 Coder Next 80B-A3B MXFP4 | 6.3 s | 1 395 tok/s | 54.3 tok/s | 46.8 GB |
| Qwen3.8 Flash Next 125B-A6B UD-IQ4_XS | 11.4 s | 713 tok/s | 41.5 tok/s | 63.5 GB |
| GPT-OSS 120B MXFP4 | 9.3 s | 1 419 tok/s | 55.3 tok/s | 61.9 GB |

With a ~2 200-token prompt, time to first token is the prompt size divided by the prompt speed (llama-server's
`prompt_ms`; 1.54 s for GPT-OSS 120B). For short prompts through the desktop proxy, Flash Next answered in 0.68 s
(chat) and 1.03 s (agent turn with a tool call), and streamed 39.2 tok/s while the RTX server generated an image.

**Image and video** (one job at a time through the engine API, a new seed per run; *cold* includes loading the
model, *warm* reuses it; times are the engine's job duration):

| Model | Settings | Cold | Warm | Memory |
|---|---|---|---|---|
| SDXL Turbo | 512², 4 steps | 11.1 s | 0.51 s | 9.9 GB |
| Qwen-Image 2.1 | 1024², 40 steps | 115 s (the run re-queued by the restart test) | 105 s | resident |
| Wan 2.2 TI2V 5B | 1280×704, 49 frames (2 s at 24 fps), 40 steps | 362 s | 327 s | 25.5 GB |
| LTX-2.3 (video + stereo audio) | 768×512, 121 frames (5 s at 24 fps), 30 steps | 631 s | 519 s | 18.2 GB |
| MiniMax H3 FP8 (video + stereo audio) | 1344×768, 124 frames (5.2 s at 24 fps), 30 steps | 1 515 s (82.5 s load) | — | 57.6 GB peak |
| Wan 2.2 T2V A14B (FP8 experts) | 832×480, 81 frames (5 s at 16 fps), 40 steps | 2 318 s (118 s load) | — | 42.3 GB |
| Wan 2.2 I2V A14B (FP8 experts) | 832×480, 81 frames, 40 steps, Qwen-Image start frame | 2 421 s | — | not isolated |

Wan 2.2 A14B at 1280×720 with 81 frames is compute-bound on GB10 (96 % GPU, no swapping): 182 s per step, so
the full 40 steps take about two hours. The catalogue therefore defaults the A14B models to 832×480; 720p stays
selectable. Loading the A14B pair takes 118–134 s (2 × 57 GB of bf16 experts read and stored in FP8).

All clips were inspected: subjects and scenes follow the prompt, LTX-2.3 and MiniMax H3 carry stereo audio
(H3: 32 kHz AAC whose two channels differ, i.e. real stereo). The I2V clip keeps its start frame faithfully but
moves very little; the steam asked for in its prompt is not visible.

## Installed but not exercised

| Model | Status |
|---|---|
| SDXL 1.0 base | Same code path as SDXL Turbo (verified); 1024² run not completed. Expect ~9–10 GB VRAM. |
| Wan 2.2 TI2V 5B | Verified on the DGX Spark (above); **untested natively on a 12 GB GPU.** Text- and image-to-video. On 12 GB it needs streamed ("offload") placement and ≥ 32 GB system RAM; expect several minutes per short clip. |
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

FLUX, SD3, LTX-Video (the original; LTX-2 is supported), HunyuanVideo, CogVideoX, Qwen-Image 1.0 (2.1 is
supported), Z-Image (only through the RTX server's gateway), PixArt-Σ and Sana pipelines are recognised but have no
native runtime adapter; the model browser flags them before download. IP-Adapter, prompt weighting syntax and
motion-control parameters are not implemented.
