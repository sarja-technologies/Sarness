# Experiments and God's Paintbrush

**Experiments** is a second top-level space next to **Work**. The **Work | Experiments** switch at the top of
the sidebar moves between them (Alt+6 opens Experiments). Work is the default, and nothing in Work changes when
Experiments is used.

* Entering Experiments always shows the **experiment selection screen** first. An experiment opens only when its
  card's button is clicked.
* An experiment you have launched stays mounted while you are back in Work. Its painting, undo history and
  settings are still there when you return. An unsaved painting is also kept as a local draft, so it survives
  an app restart.
* Experiments are registered in `desktop/src/lib/experiments/registry.ts` (desktop) and
  `engine/sarness_engine/experiments/registry.py` (engine). Adding one means adding a registry entry and a
  lazily loaded component; no shell code has to change.

The only experiment installed is **God's Paintbrush**. It is an NVIDIA Canvas-style landscape painter: you paint
with semantic materials and the server returns a photorealistic landscape that follows the painting.

## Using God's Paintbrush

1. Click **Experiments** → **Try God's Paintbrush**.
2. Pick a material (Sky, Water, Mountains, …) and paint on the left. The live GauGAN image on the right updates
   while you paint.
3. When you pause, a refined, photographic version replaces it. You can also click **Generate photo**
   (Ctrl+Enter), and **New variation** gives a new seed.
4. Change the look with **Style** (golden hour, overcast, winter, autumn, misty, sunset, night) or with your own
   text guidance. The other refine settings are quality, creativity, output scale, *Follow the painting* and
   the seed.
5. **Save** (Ctrl+S) stores the painting on the server. **Save to Library** puts the landscape into the normal
   Library (tag `#gods-paintbrush`), and **Export** writes the image or the material mask to disk.

Canvas tools:
* **Painting:** brush (B), eraser (E), fill (G), picker (I), hand/pan (H or Space), brush size ([ and ]).
* **Navigation:** wheel zoom, middle-button pan.
* **History:** undo/redo (Ctrl+Z / Ctrl+Y, 60 steps).
* **Layers:** layers with visibility, rename, reorder and clear, plus a background material.

Paintings can be created at four sizes: 768×512, 512×512, 896×512 and 512×768. They are opened, duplicated,
renamed and deleted from the sidebar or the **Open** dialog.

## Architecture

```
Desktop (Svelte)                     Engine API process (no torch)                Experiment worker (subprocess)
───────────────────                  ─────────────────────────────                ──────────────────────────────
LabelCanvas (Uint8 label layers)     /v1/experiments/gods-paintbrush/render       PaintbrushHandler
  │ composite → zlib → base64   ───► decode + validate mask                        ├─ live:   SPADE generator (fp16)
PaintSession                          LatestWinsScheduler (per canvas session) ──► └─ refine: SD 1.5 + ControlNet-Seg
  · at most 1 live request in flight  GPU politeness (jobs / gateway / chat)          (img2img over the live frame)
  · refine after a 900 ms pause      render cache (12) → save / export             idle unload, VRAM accounting
```

* **Material indices, not colours.** The desktop paints *material indices* (1…20) into `Uint8Array` layers, and
  the colours on screen are only a lookup table. The composite label map is sent as
  `{width, height, encoding: "deflate", data: <base64 zlib>}`, which is typically a few hundred bytes. The
  engine maps each index separately to the label protocol of each model (see *Materials*). Spatial
  correspondence is therefore exact: the live model and the ControlNet both receive the label map
  pixel-for-pixel.
* **Two stages:**
  * **Live:** SPADE/GauGAN takes the COCO-Stuff one-hot map. A frame takes about 70 ms, so it updates while
    you paint.
  * **Refine:** ControlNet-Seg on SD 1.5, run as img2img over the live frame. The control image is the ADE20K
    colour map, with a prompt built from the painted materials (by area) plus your guidance. This gives
    photographic detail, seeds and text guidance.
* **Scheduling.** Requests carry a canvas `session`. Within a session only the newest request is kept: an older
  queued request is answered `409 superseded`, and a running refine is cancelled between denoising steps. The
  desktop never has more than one live request in flight, and skips frames while one is pending.
* **Isolation.** All torch code runs in a separate worker process (`experiments/worker.py`). This is the same
  pattern the image/video GPU worker uses. The API process never imports torch, and a crash in the worker
  surfaces as an error without taking the engine down.
* **Persistence.** Saved paintings are rows in the `experiment_documents` table (DB migration 2). Renders live
  in an in-memory cache of 12. *Save to Library* turns a render into an ordinary generation, storing the image,
  the COCO-Stuff mask and the full parameters.

### GPU sharing

The paintbrush is designed never to get in the way of the rest of Sarness:

| Situation | What happens |
|---|---|
| A Sarness image/video job is queued | Job dispatch calls `yield_gpu`: queued paintbrush requests are cancelled, the worker stops and frees its VRAM, and new paint requests get `503` "GPU busy" for the job's duration plus 10 s. The desktop shows "Waiting for the GPU" and retries every 4 s. |
| The AI gateway starts a GPU job for someone else | Housekeeping checks the gateway every 3 s. If the paintbrush is idle, it yields the same way. |
| VRAM is short when loading | The paintbrush first releases idle Sarness components. It then asks the gateway to unload idle Ollama chat models, but only if no chat reply is streaming. |
| A chat model is loading | The chat service asks the paintbrush to yield first. |
| No painting for 3 min | The refine models (SD 1.5 + ControlNet) are released, and about 0.5 GB stays resident. |
| No painting for 10 min | The worker exits and all of its VRAM is returned. |

Both idle timeouts are settings: `experiments_refine_idle_minutes` and `experiments_worker_idle_minutes`
(`PATCH /v1/settings`, 0 disables).

## Model choice

Requirements:
* a pretrained model;
* real semantic-map → photo translation with spatial correspondence;
* interactive speed on a 12 GB RTX 4070 Ti;
* weights with a verifiable source and licence;
* weights small enough to sit next to the server's existing image/video/chat models.

| Candidate | What it is | Verdict |
|---|---|---|
| **NVIDIA Canvas / GauGAN2** | NVIDIA's own app and model | Closed: a Windows-only RTX app with no downloadable weights and no licence to embed. Not usable. |
| **NVlabs SPADE official checkpoints** | SPADE trained on COCO-Stuff, ADE20K and Cityscapes | Real, and released under CC BY-NC-SA 4.0, but trained on scene datasets, not landscapes. NVIDIA never released the Flickr landscape model behind the GauGAN demo. Not tested here. |
| **SPADE "Labels2Landscapes" 512 (community)** | The official SPADE code trained for 50 epochs on ~41k Flickr landscapes with COCO-Stuff labels. Shared in [NVlabs/SPADE#17](https://github.com/NVlabs/SPADE/issues/17), used by [mcheng89/gaugan](https://github.com/mcheng89/gaugan). | **Chosen for live preview.** It is the closest public equivalent of GauGAN's landscape model, with the same architecture and the same label space as Canvas's materials. Measured at 62 ms of GPU time at 768×512, 1.1 GB peak. The licence is inherited from SPADE: **CC BY-NC-SA 4.0, non-commercial**. |
| **ControlNet 1.1 Seg + SD 1.5** | [lllyasviel/control_v11p_sd15_seg](https://huggingface.co/lllyasviel/control_v11p_sd15_seg) on [stable-diffusion-v1-5](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5), ADE20K protocol | **Chosen for refine.** It is photographic, seedable and text-guidable, and runs at 1.3 s at 768×512 with 20 steps. It follows a segmentation map well, but on its own (txt2img) it invents textures that a painter does not ask for. Used as img2img over the SPADE frame, it keeps GauGAN's layout and colours and adds detail. Licence: CreativeML OpenRAIL-M. |
| SDXL / FLUX segmentation ControlNets | Community checkpoints | No official segmentation ControlNet exists for SDXL or FLUX. Community ones vary in protocol and licence, and SDXL alone needs about 8–10 GB, which would crowd out the server's other models. Rejected. |
| T2I-Adapter seg (SD 1.4) | Lighter control adapter | Older, and published comparisons show weaker spatial adherence than ControlNet 1.1 Seg. Not tested here. |

Prototype numbers on the server's RTX 4070 Ti (fp16, before integration):

| Configuration | Result |
|---|---|
| SPADE live 512×512 (fp16 vs fp32) | ~70–110 ms per frame. The fp16 output matches fp32 within one grey level (mean abs diff 0.03), so fp16 is used. |
| ControlNet txt2img 512×512, 8 / 20 steps | 0.54 / 1.24 s, peak 3.4 GB |
| ControlNet txt2img 768×512, 8 / 20 steps | 0.75 / 1.69 s, peak 3.7 GB |
| ControlNet img2img 512×512, strength 0.45 / 0.65 | 0.64 / 0.87 s, peak 3.4 GB |
| ControlNet img2img 1024×1024, strength 0.45 / 0.65 | 3.0 / 4.1 s, peak 5.3 GB |

### Weights, provenance and licences

The weights are installed from the experiment's model panel and are checked against pinned revisions and
SHA-256 digests. They live under `<models_dir>/experiments/gods-paintbrush/<component>/`, outside the folders
the model registry scans, so they never appear as ordinary image models.

| Component | Source | Pinned | Size | Licence |
|---|---|---|---|---|
| `spade-landscapes-512` | Google Drive files linked from NVlabs/SPADE#17 (`latest_net_G.pth`, `opt.txt`) | SHA-256 `9dc754be…167a` | 390 MB | **CC BY-NC-SA 4.0** (non-commercial) |
| `sd15` | `stable-diffusion-v1-5/stable-diffusion-v1-5` (fp16 text encoder, UNet, VAE) | rev `451f4fe1`, UNet SHA-256 `c8390825…ea21` | 2.1 GB | CreativeML OpenRAIL-M |
| `controlnet-seg` | `lllyasviel/control_v11p_sd15_seg` (fp16) | rev `21068204`, SHA-256 `23809d29…f6f4` | 0.7 GB | CreativeML OpenRAIL-M |

> **Non-commercial.** Because of the SPADE weights' licence, God's Paintbrush as shipped is for personal and
> research use. The install panel says so. A commercial deployment would need a live model with a different
> licence, or a refine-only mode.

## Materials

The palette has 20 materials: all 16 requested plus Fog, Hills and Road. A material's *identity* is its id. The
colour is only an interface hint and can change without affecting results. Each material maps to one class in
each model:

| Material | Live (COCO-Stuff) | Refine (ADE20K) | Status |
|---|---|---|---|
| Sky | 156 sky-other | 3 sky | native |
| Clouds | 105 clouds | 3 sky + "clouds" in prompt | refine **approximate** (ADE20K has no cloud class) |
| Fog | 119 fog | unlabelled + "fog, mist" | refine **approximate** |
| Water | 177 water-other | 22 water | native |
| Ocean | 154 sea | 27 sea | native |
| River | 147 river | 61 river | native |
| Grass | 123 grass | 10 grass | native |
| Trees | 168 tree | 5 tree | native |
| Forest | 168 tree | 5 tree + "a dense forest" | **approximate** (no forest class in either model) |
| Bushes | 96 bush | 18 plant | native |
| Flowers | 118 flower | 67 flower | native |
| Mountains | 134 mountain | 17 mountain | native |
| Hills | 126 hill | 69 hill | native |
| Rocks | 149 rock | 35 rock | native |
| Snow | 158 snow | unlabelled + "snow" | refine **approximate** (ADE20K has no snow class) |
| Sand | 153 sand | 47 sand | native |
| Dirt | 110 dirt | 14 earth | native |
| Road | 148 road | 7 road | native |
| Buildings | 95 building-other | 2 building | live **experimental**: the landscape training photos contain few buildings, so the live frame renders them loosely |

The picker marks approximate materials with ≈ and experimental ones with *beta*. Its tooltip names the exact
class used by each model.

## Measured performance (RTX 4070 Ti 12 GB, the Sarness AI server)

Measured with `engine/tests/paintbrush_e2e.py` against a running engine (median of 20 runs, wall clock seen by
the HTTP client). Driver 595, PyTorch 2.11 (CUDA 12.8), diffusers 0.41. The GPU baseline before the run was 33 MB
in use.

**Cold start.** The first live frame took 3.5 s from a stopped worker: process spawn, 0.86 s model load and a
0.35 s first frame. Loading the refine models takes another 3.1 s.

**Live preview (GauGAN):**

| Canvas | Wall median (p95) | GPU time | Peak VRAM (allocated) |
|---|---|---|---|
| 512×512 | 48 ms (48) | 41 ms | 0.8 GB |
| **768×512** (default) | **72 ms (73)** | 62 ms | 1.1 GB |
| 896×512 | 83 ms (85) | 73 ms | 1.2 GB |
| 512×768 | 72 ms (74) | 62 ms | 1.1 GB |
| 1024×1024 | 193 ms (249) | 175 ms | 2.6 GB |

At the default size that is roughly 14 frames per second end to end. In practice the desktop renders the newest
state after each stroke segment: 16 rapid edits produced 5 renders and 11 superseded requests in 349 ms.

**Refine (SD 1.5 + ControlNet-Seg, img2img, creativity 0.7):**

| Setting | Wall median | GPU time | Peak (allocated) | Worker VRAM |
|---|---|---|---|---|
| Draft, 768×512, 10 steps | 0.88 s | 0.77 s | 3.9 GB | 4.4 GB |
| **Standard, 768×512, 20 steps** | **1.36 s** | 1.31 s | 3.9 GB | 4.4 GB |
| High, 768×512, 30 steps | 1.93 s | 1.84 s | 3.9 GB | 4.4 GB |
| Standard, 1.5× (1152×768) | 3.77 s | 3.61 s | 5.1 GB | 5.9 GB |
| Standard, 2.0× (1536×1024) | 8.32 s | 8.10 s | 4.1 GB (VAE tiling) | 5.9 GB |

**Idle VRAM.**
* Live model loaded: about 1.4 GB.
* Refine loaded: about 3.3 GB, rising to 4.4 GB after refines (cached allocator blocks).
* After the 3-minute refine release: 0.5 GB.
* After the worker exits: back to the 33 MB baseline.

**Other measurements.**
* **Cancellation.** A running refine stops within 470 ms of `/cancel` or of a newer request.
* **Determinism.** The same seed reproduces the image exactly (mean abs diff 0.0); a new seed gives a visibly
  different variation (18.8).
* **Coexistence.**
  * With the paintbrush loaded, a `qwen3.5:9b` chat answered normally through the gateway.
  * A `gw:qwen-image-2.1-turbo` image job completed while the paintbrush stood down.
  * Painting resumed afterwards with a 4 s worker re-spawn.

### Findings from tuning

* **cuDNN autotuning is off.** `torch.backends.cudnn.benchmark = True` re-tunes for every new canvas or output
  shape. With it on, the first live frame took 10.5 s and the first refine 22 s, compared with 0.34 s and 1.3 s
  with it off. The steady-state gain was only about 2 %, and painting changes shapes constantly.
* **Creativity defaults to 0.7.** A sweep at 0.55, 0.7, 0.85 and 1.0, each with no guidance, "winter" and
  "autumn", showed:
  * The painted layout holds at every value.
  * Below about 0.7 the result stays close to the live frame and ignores text guidance.
  * From about 0.85 up, guidance can change the season and light: the mean abs diff against the unguided image
    was 23.4 at 0.85.
  * The style presets therefore raise creativity to 0.85–0.9, and the guidance box shows a hint when creativity
    is below 0.8.

## How close is this to NVIDIA Canvas?

| Canvas feature | God's Paintbrush |
|---|---|
| Paint with materials, see a landscape immediately | Yes, using the same model family (GauGAN/SPADE). Live frames take ~72 ms on the server, plus network time. |
| Brush, eraser, fill, picker, brush size, undo/redo, layers | Yes |
| Styles / reference images | Text styles and presets plus free-form guidance. No reference-image style transfer. |
| Variations | Yes, by seed, with an exact repeat for the same seed |
| Output quality | The live frame comes from a 512 px community GauGAN model and is softer than a finished photo. Refine adds photographic detail: 768×512 in 1.4 s, up to 1536×1024. Canvas itself was not available for a side-by-side comparison. |
| Runs offline on your own GPU | Yes, but on the Sarness server, not the desktop |
| Panorama / 360° export, PSD export | No |
| Materials | 20. Snow/clouds/fog are approximate in refine; buildings are weak in the live frame. |

## API

All endpoints require the bearer token. See [API.md](API.md#experiments) for the table. Render request:

```json
POST /v1/experiments/gods-paintbrush/render
{
  "session": "canvas-1",
  "kind": "live" | "refine",
  "mask": {"width": 768, "height": 512, "encoding": "deflate", "data": "<base64 zlib of uint8 indices 1..20>"},
  "seed": -1, "quality": "draft|standard|high", "creativity": 0.7, "control": 1.0, "scale": 1.0,
  "prompt": "golden hour, warm sunlight"
}
```

The response is `{render_id, session, kind, width, height, mime, image (data URL), model, timings: {queue_ms,
load_ms, live_ms|refine_ms, total_ms}, peak_vram_mb, coverage}`. Refines also return `seed, prompt, guidance,
quality, steps, creativity, control, scale`. Use `render_id` with `…/renders/{id}/save` (into the Library) or
`…/renders/{id}/export` (a downloadable file). Status codes:
* **409** `superseded` or `cancelled`: a newer request from the same session replaced this one, or `/cancel`
  stopped it. Not an error.
* **409** with an install message: the weights are missing.
* **503** `GPU busy`: an image or video job has the GPU.
* **422**: the mask is invalid.

Progress is published on the events WebSocket as `experiment.progress {experiment, render_id, session, step,
total}`.
Status changes go out as `experiment.status`, saved paintings as `experiment.documents`, and model installs as
`experiment.install`.

### Saved painting format (`sarness.paintbrush` v1)

```json
{
  "format": "sarness.paintbrush", "version": 1, "width": 768, "height": 512,
  "background": "sky",
  "palette": [{"index": 1, "id": "sky", "color": "#8ec5f4"}, …],
  "layers": [{"id": "…", "name": "Base", "visible": true, "encoding": "deflate", "data": "<base64>"}],
  "settings": {"live": true, "autoRefine": true, "quality": "standard", "creativity": 0.7, "control": 1.0,
               "scale": 1.0, "seed": -1, "guidance": ""},
  "model": {"live": "spade-landscapes-512", "refine": "sd15-controlnet-seg"}
}
```

* In layer pixels, 0 means *unpainted*, so the layers below or the background show through.
* Loading remaps indices **by material id** through the stored palette, so old paintings stay correct if
  materials are reordered or added.
* A material this engine does not know is cleared to the background, and the app reports it.
* Documents are stored with a thumbnail and can be listed, duplicated, renamed and deleted through
  `/v1/experiments/{id}/documents`.

Mask export (`POST …/mask/export {mask, format}`) writes a PNG in one of four encodings:
* `index`: raw material indices;
* `coco`: COCO-Stuff class ids, as SPADE consumes them;
* `ade`: ADE20K colours, as ControlNet consumes them;
* `color`: the interface colours.

Each export comes with a legend.

## Tests

* **Engine** (`engine/tests/test_experiments.py`, CPU, 45 tests). Covered: registry and catalogue; mask
  decoding and validation; material mappings; prompt building; the latest-wins scheduler; preemption and
  cancellation; worker crash recovery; GPU yielding to jobs; idle release; document CRUD and migration; render
  save/export. These tests use a fake handler (`tests/fake_experiment_handler.py`) instead of torch.
* **Desktop** (`engine.test.ts`, `doc.test.ts`). Covered: label painting, lines, span fill, layer compositing,
  undo/redo and its cap, zlib mask encoding that Python reads back, and the document round trip, including
  material remapping.
* **GPU end to end** (`engine/tests/paintbrush_e2e.py`, run on the server against a running engine). Covered:
  basics, live latency at all sizes, a per-material contact sheet, refine at every quality and scale, seed
  determinism and guidance, cancellation, VRAM release, and coexistence with chat and an image job (`--coexist`).
  It writes `results.json` and the images to `--out`.

```
SARNESS_URL=http://127.0.0.1:8742 SARNESS_TOKEN_FILE=~/token python tests/paintbrush_e2e.py --out e2e --coexist
```
