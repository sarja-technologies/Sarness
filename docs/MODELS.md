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

### Model expansion (October 2026)

Benchmarks are repeatable: `bench2.py` drives the engine API with fixed prompts and seeds (seven scenes × the
*none* and *iPhone* styles for images; one harbour scene for video; twelve prompts for chat models) and records
load time, sampling time and the drop in available system memory (*memory*; PyTorch's own peak counts every model
still cached in the worker, so it is not used here). Every output was looked at.

**Text to image** (warm = model already loaded):

| Model | Licence | Settings | Load | Per image | Memory | Verdict |
|---|---|---|---|---|---|---|
| Qwen-Image-2512 | Apache-2.0 | 1328², 50 steps, true CFG 4 | 65 s | 272 s | 55 GB | Most natural faces and skin; signs spelled correctly |
| Qwen-Image-2512 + Lightning LoRA (lightx2v, Apache-2.0) | | 1328², 8 steps, CFG 1 | — | 28–31 s | 55 GB | Close to the 50-step image at 9× the speed; slightly glossier light |
| Qwen-Image 2.1 | Apache-2.0 | 1024², 40 steps | 38 s | 107 s | 28 GB | Excellent: correct text, real hands, convincing iPhone style |
| Z-Image-Turbo | Apache-2.0 | 1024², 9 steps, no CFG | 40 s | 12.4 s | 32 GB while loading, 22 GB resident | Perfect text and hands; iPhone style keeps some background blur |
| Z-Image-Turbo + Realism LoRA (suayptalha, Apache-2.0) | | trigger "Realism" | — | 14.6–15.4 s | +0.1 GB | Works, but not clearly more realistic (snapshot look, odd details); optional only |
| FLUX.2 Klein 4B | Apache-2.0 | 1024², 4 steps | 18 s | 3.4 s | 19 GB | Best iPhone look for its speed; plain-style text has errors; hands weaker |

The *iPhone / candid photo* style (Styles menu, all image families) rewrites the prompt and negative prompt; it was
tested on every model above (56 images in all) and gives deeper focus, phone-like exposure and less polish.

**Editing, image-to-image and inpainting** (1024², bf16; source images from the run above):

| Model | Task | Time | Memory | Result |
|---|---|---|---|---|
| Qwen-Image-Edit-2511 (Apache-2.0) | one reference (mugs → wine, evening light) | 278 s (+64 s load) | 55 GB | Faces, clothes and layout kept; every mug replaced |
| Qwen-Image-Edit-2511 | two references (person into another room) | 455 s | | Identity kept; the room is only loosely the second image's |
| Qwen-Image-Edit-2511 | change a sign's text | 277 s | | Exact new text in the same handwriting |
| FLUX.2 Klein 4B | the same three edits | 6.6–10.4 s | 19 GB | Good single edit; the sign text came out wrong; second reference pasted in rather than combined |
| Z-Image-Turbo | img2img (strength 0.55, iPhone style) | 7.8 s | 34 GB | Composition kept, faces lightly changed |
| Qwen-Image-2512 | img2img (strength 0.6) | 98 s | 55 GB | Composition kept; faces change more |
| Z-Image-Turbo / FLUX.2 Klein 4B / Qwen-Image-2512 | inpaint (strength 0.9, cake into a masked table) | 12.8 s / 6.7 s / 114 s | 28 / 16 / 55 GB | Z-Image and Klein: a convincing lit cake, rest untouched; Qwen: a small cake on a tall stand, no candles |

**Video** (one harbour scene and one start frame; 121 frames = 5 s at 24 fps; *sampling* excludes loading):

| Model | Task | Settings | Load | Sampling | Result |
|---|---|---|---|---|---|
| LTX-2.3 (video + stereo audio) | text to video | 1024×576 (16:9), 30 steps | 111 s | 781 s | Sharp, natural motion; stereo audio track |
| LTX-2.3 | text to video | 576×1024 (9:16), 30 steps | — | 786 s | Same quality in portrait |
| LTX-2.3 | image to video | 1024×576, 30 steps, Z-Image start frame | 109 s | 884 s | Start frame and identity kept; she looks up as a red bus pulls in, but keeps holding the phone instead of putting it away; stereo audio, all 121 frames |
| HunyuanVideo 1.5 480p | text to video | 848×480, 50 steps, CFG 6 | 59 s | 4 332 s (87 s/step) | Coherent scene and motion, no audio |
| HunyuanVideo 1.5 480p | image to video | 848×480, 50 steps, CFG 6, Z-Image start frame | 60 s | 3 069 s (61 s/step) | Start frame and identity kept; follows the whole prompt (looks up, bus pulls in behind her in the rain, phone put away); the bus display's text is garbled; 64 GB, no audio |

HunyuanVideo 1.5 hides padded text tokens with a dense sequence × sequence mask, which rules out flash attention:
at 848×480 (~50 k tokens) each step took ~150 s. The engine now attends over only the valid tokens (same result,
checked against the masked kernel): 61 s/step (the text-to-video run still carried an expensive mask check, since
removed). The image-to-video pipeline takes no width or height and sizes its output from the start frame and the
checkpoint's 720p `target_size`; the engine sets `target_size` from the requested area, so 848×480 stays 848×480
(before, it silently ran at 1264×720 and ~270 s/step). 720p and 9:16 Hunyuan runs are in the benchmark suite but
were deferred (1–3 h of GPU time each).

**Low-refusal chat models** (llama.cpp, context 32 768; twelve prompts: two reasoning, coding checked by running the
code, instruction following, knowledge, a 200-word story, six borderline requests that stock models often refuse;
memory is what the loaded model takes):

| Model | Load | Memory | Generation | Reasoning | Code | Instructions | Knowledge | Story length | Answered (of 6) |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3.8 27B (original, UD-Q6_K) | 6.5 s | 27 GB | 21.4 tok/s | 2/2 | ✓ | ✓ | ✓ | 277 words | 5 (refused lock-picking) |
| Huihui Qwen3.8 27B abliterated Q6_K | 6.2 s | 26 GB | 20.4 tok/s (MTP; 9.7 without) | 2/2 | ✓ | ✓ | ✓ | 267 | 6 |
| Qwen3.8 27B Uncensored HauhauCS Q6_K_P | 6.9 s | 30 GB | 18.2 tok/s (MTP; 8.7 without) | 2/2 | ✓ | ✓ | ✓ | 251 ✓ | 6 |
| Huihui Qwen3.5 27B abliterated Q6_K | 5.8 s | 25 GB | 9.7 tok/s | 2/2 | ✓ | ✓ | ✓ | 277 | 6 |
| Qwen3.8 Flash Next (original, UD-IQ4_XS) | 10.6 s | 68 GB | 38.4 tok/s | 2/2 | ✓ | ✓ | ✓ | 230 ✓ | 5 (refused lock-picking) |
| Huihui Qwen3.8 Flash Next abliterated UD-IQ4_XS | 8.4 s | 63 GB | 28.8 tok/s | 2/2 | ✓ | ✓ | ✓ | 271 | 6 |
| Dolphin 3.0 R1 Mistral 24B Q6_K | 4.1 s | 24 GB | 11.3 tok/s | 2/2 | ✗ | ✗ | ✗ | 565 | 6 |
| Hermes 4 70B Q4_K_M | 7.4 s | 51 GB | 4.9 tok/s | 0/2 | ✓ | ✓ | ✓ | 263 | 6 |

The original Qwen3.8 27B and Flash Next come with separate multi-token-prediction (MTP) draft files that the engine
uses for speculative decoding (37–73 % of drafted tokens accepted). The HauhauCS and Huihui Qwen3.8 27B GGUFs carry
the MTP block inside the model file (`blk.64.nextn.*`); the engine detects it from the header and drafts with it too,
which doubled their speed with the same answers (all checks identical with and without; +1.2 GB). Huihui Qwen3.5 27B
and the Huihui Flash Next conversion have no MTP block, which is why Flash Next abliterated stays 1.3× slower than
the original. For Qwen3.5 0.8B, which also embeds one, MTP gives 176–255 tok/s against 207 without (slower on prose,
faster on code and lists). The stock models answered the drug-harm-reduction request with a disclaimer and refused only the
lock-picking one; the low-refusal models answered all six without disclaimers. Prompt processing for these short
prompts took 0.13–0.46 s. Recommended: **Huihui Qwen3.8 Flash Next abliterated** (fast, strongest) and **HauhauCS
Qwen3.8 27B** (best of the 27B class, 18 tok/s, fits beside an image model).

**Switching between chat and image models.** With Flash Next abliterated (63 GB) loaded, a Qwen-Image-2512 job
(55 GB, 1024², 8 Lightning steps) unloaded the idle chat model and finished in 88 s; loading the chat model again
stopped the image worker and took 10.4 s. Available memory never fell below 55 GB and nothing was killed.

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

FLUX.1, SD3, LTX-Video (the original; LTX-2 is supported), the original HunyuanVideo (1.5 is supported),
CogVideoX, Qwen-Image 1.0 (2512, 2.1 and Edit-2511 are supported), PixArt-Σ and Sana pipelines are recognised but
have no native runtime adapter; the model browser flags them before download. FLUX.2 [dev], FLUX.2 Klein 9B and
other gated repositories need the user to accept the licence on Hugging Face and add a token in Settings; they are
not installed. IP-Adapter, prompt weighting syntax and
motion-control parameters are not implemented.
