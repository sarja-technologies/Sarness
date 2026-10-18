# Sarness Engine API v1

Base URL: `http://<host>:8742` (default port). All endpoints are under `/v1`.
Interactive OpenAPI docs: `GET /v1/docs` (requires a token in the browser's
`Authorization` header, or use `curl`).

Versioning: `GET /v1/health` reports `api_version` (`major.minor`). Clients must
refuse a different **major** version; minor versions only add fields/endpoints.

## Authentication

Every endpoint except `/v1/health` requires `Authorization: Bearer <token>`.
Tokens are created by `sarness-engine init` / `sarness-engine token --rotate` and
stored server-side only as a SHA-256 hash. Twenty failed attempts per minute
from one address return `429`. `/v1/files/{id}` additionally accepts short-lived
signed URLs (`POST /v1/files/{id}/sign`). The WebSocket accepts the bearer header
or a `token` query parameter (the desktop proxy always uses the header). Query
strings are never written to logs.

Errors are JSON: `{"detail": "...", "issues": [...]}` (issues only for workflow
validation, status `422`).

## System

| Method | Path | Description |
|---|---|---|
| GET | `/v1/health` | Liveness, versions. No auth, no machine details. |
| GET | `/v1/system` | Hardware (GPU/VRAM/RAM/CPU/storage), runtime versions (torch, CUDA, diffusers, llama.cpp), worker state, chat model state, job counts, health issues. |
| GET / PATCH | `/v1/settings` | Runtime settings. Secrets (`hf_token`, endpoint `api_key`) are write-only and read back as booleans. |
| GET | `/v1/runtimes` | Installed runtimes and versions. |
| POST | `/v1/runtimes/llama/install` | `{variant?: "cuda-12.8"\|"vulkan"\|"cpu"}` — downloads the official llama.cpp release in the background. |
| POST | `/v1/worker/release` · `/v1/worker/stop` | Drop cached diffusion models / stop the GPU worker (frees all VRAM). |

## Models

| Method | Path | Description |
|---|---|---|
| GET | `/v1/models?category=` | Installed models with capabilities, limits, defaults, VRAM estimates, `loaded`. |
| POST | `/v1/models/rescan` | Rescan model folders. |
| DELETE | `/v1/models/{id}` | Delete model files (only inside the managed models directory). |
| GET | `/v1/models/catalog` | Curated catalog with install state and per-GPU compatibility. |
| GET | `/v1/models/presets` | Hardware presets (8/12/16/24 GB) and which matches this server. |
| GET | `/v1/models/disk` | Disk usage by category and free space. |
| POST | `/v1/models/install` | `{catalog_id, quant?}` or `{repo, files[], category, name?}` → download row. Validates disk space (2 GB headroom). |
| GET | `/v1/downloads` | Download rows (`queued|downloading|paused|completed|failed|cancelled`). |
| POST | `/v1/downloads/{id}/{pause|resume|cancel|dismiss}` | Control a download (HTTP Range resume). |
| GET | `/v1/hf/search?q=&kind=llm|image|video|lora` | Hugging Face search. |
| GET | `/v1/hf/inspect?repo=owner/name` | Format detection, quantization options, sizes, compatibility, support verdict. |

## LLM

| Method | Path | Description |
|---|---|---|
| GET | `/v1/llm/status` | `stopped|loading|ready|error`, model, ctx, vision, active streams. |
| GET | `/v1/llm/models` | Chat-capable models: local GGUFs (`local:<id>`) and remote endpoint models (`endpoint:<id>:<model>`). |
| POST | `/v1/llm/load` · `/v1/llm/unload` | `{model_id, ctx?, gpu_layers?}`. Loading stops an idle diffusion worker if VRAM is short. |
| POST | `/v1/llm/endpoints/{id}/refresh` | List models of a remote OpenAI-compatible endpoint. |

## Chat

| Method | Path | Description |
|---|---|---|
| GET / POST | `/v1/chat/conversations` | List (`q`, `project_id`) / create (`title, model_ref, system_prompt, params, project_id, folder_id`). |
| GET / PATCH / DELETE | `/v1/chat/conversations/{id}` | Full message tree / rename, pin, move, set `active_leaf_id`, params. |
| POST | `/v1/chat/conversations/{id}/generate` | **Server-Sent Events.** Body: `{mode: "send"|"regenerate"|"edit", content?, parent_id?, edit_of?, attachments?: [file_id], model_ref?, params?}`. |
| POST | `/v1/chat/conversations/{id}/stop` | Stop the active generation (partial text is kept). |
| DELETE | `/v1/chat/conversations/{id}/messages/{mid}` | Delete a branch (message and descendants). |
| GET | `/v1/chat/conversations/{id}/export?format=json|md` | Export. |
| POST | `/v1/chat/import` | Import a `sarness.conversation` export. |

Messages form a tree via `parent_id`; *regenerate* and *edit* create siblings, so
nothing is ever overwritten. SSE events (each `data: <json>`):

```
{"type":"user_message","message":{…}}
{"type":"title","title":"…"}
{"type":"assistant_start","message":{…}}
{"type":"status","text":"Loading model…"}
{"type":"delta","content":"…","reasoning":"…"}
{"type":"error","message":"…"}
{"type":"done","message":{…, "stats":{"tokens_per_s","completion_tokens","prompt_tokens","ttft_s","elapsed_s","finish_reason"}}}
```

`params`: `temperature, top_p, top_k, min_p, repeat_penalty, presence_penalty,
frequency_penalty, max_tokens, seed, ctx, gpu_layers, thinking`.

## Jobs and workflows

| Method | Path | Description |
|---|---|---|
| POST | `/v1/jobs` | `{kind: "image"|"video", simple?: {...}, workflow?: Workflow, project_id?, folder_id?, title?}` |
| GET | `/v1/jobs?status=queued,running&kind=&limit=` | Includes `queue_position` and `waiting_reason`. |
| GET | `/v1/jobs/{id}` | Job with its generations and per-node states. |
| POST | `/v1/jobs/{id}/cancel` · `/v1/jobs/{id}/retry` | Cancel / clone (`{new_seed?: bool}`). |
| DELETE | `/v1/jobs/finished` | Remove finished jobs that produced no generations. |
| GET | `/v1/nodes` | Node library: socket types/colours, categories, node specs (inputs, outputs, params with ranges/options). |
| POST | `/v1/workflows/validate` | `{workflow}` → `{valid, issues[]}`. |
| POST | `/v1/workflows/compile` | `{kind, simple}` → Workflow (what simple mode runs). |
| POST | `/v1/workflows/simplify` | `{workflow}` → `{representable, simple, reasons[]}`. |
| GET / POST | `/v1/workflows` | Saved workflows and presets (`is_preset`). |
| GET / PUT / DELETE | `/v1/workflows/{id}` | |

Simple image parameters: `model, prompt, negative_prompt, width, height,
batch_size, steps, guidance, seed (−1 = random), scheduler, denoise,
reference_image (file id), mask_image (file id), loras [{id, strength}], vae,
memory_mode (auto|gpu|offload|sequential), upscale {model, factor}, format
(png|webp|jpeg), clip_skip`.

Simple video parameters: `model, prompt, negative_prompt, width, height, frames,
fps, steps, guidance, seed, flow_shift, start_image, end_image, memory_mode
(auto|gpu|split|offload|sequential), format (mp4|webm|gif)`.

## Library

| Method | Path | Description |
|---|---|---|
| GET | `/v1/generations` | Filters: `type, project_id ("none" = unfiled), folder_id, q, model_id, favorite, tag, collection_id, since, until, job_id, trash`; cursor pagination with `before`. |
| GET / PATCH / DELETE | `/v1/generations/{id}` | Detail incl. workflow / `{favorite, project_id, folder_id, tags[]}` / trash (`?permanent=true` deletes files). |
| POST | `/v1/generations/bulk` | `{ids[], action: move|favorite|trash|restore|tag, …}` |
| POST | `/v1/generations/{id}/rerun` | `{new_seed?: bool}` — re-queues the exact stored workflow. |
| GET | `/v1/generations/{id}/metadata` | `sarness.generation` JSON export. |
| GET | `/v1/tags` · `/v1/library/models` | Facets. |
| … | `/v1/collections[/{id}[/items[/{gid}]]]` | Collections CRUD and membership. |
| … | `/v1/projects[/{id}]`, `/v1/folders[/{id}]` | Projects and nested folders (cycle-safe moves; deleting keeps contents as unfiled). |
| GET | `/v1/files/{id}` | File content (Range supported, immutable caching). |
| POST | `/v1/uploads` | Multipart upload; content is sniffed (PNG/JPEG/WebP/GIF/BMP, MP4/WebM/MOV, text/JSON/CSV/PDF), images are verified. 512 MB limit. |

## Experiments

Interactive experiments (see [EXPERIMENTS.md](EXPERIMENTS.md)). Added in API 1.1.

| Method | Path | Description |
|---|---|---|
| GET | `/v1/experiments` | Registered experiments: `id, name, tagline, description, version, capabilities[], availability {state: ready\|needs_install\|unavailable, live, refine, reasons[]}, gpu_active`. |
| GET | `/v1/experiments/{id}` | Full status of one experiment (same as its own `/status`). |
| GET / POST | `/v1/experiments/{id}/documents` | Saved documents (newest first, no `data`) / create `{title, data, thumbnail?, project_id?}`. 8 MB data and 512 KB thumbnail limits. |
| GET / PUT / DELETE | `/v1/experiments/{id}/documents/{doc_id}` | Full document / update `{title?, data?, thumbnail?, project_id?}` / delete. |
| POST | `/v1/experiments/{id}/documents/{doc_id}/duplicate` | `{title?}` → the copy. |

God's Paintbrush (`/v1/experiments/gods-paintbrush/…`):

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Materials (index, id, label, colour, COCO-Stuff and ADE20K mapping, `live`/`refine` support, note), model components with licence/source/installed, canvas sizes, refine limits and defaults, runtime (worker alive, loaded stages, VRAM, GPU busy reason, recent latency). |
| POST | `/render` | `{session, kind: "live"\|"refine", mask: {width, height, encoding: "deflate"\|"raw", data}, seed?, quality?, creativity?, control?, scale?, prompt?}` → `{render_id, image (data URL), timings, peak_vram_mb, coverage, …}`. `409` superseded/cancelled, `503` GPU busy, `422` invalid mask. |
| POST | `/cancel` | `{session}` — cancel queued and running renders of that canvas. |
| POST | `/warmup` · `/release` | `{refine?: bool}` load models ahead of time / `{level: "refine"\|"all"}` free VRAM. |
| POST | `/install` · `/install/cancel` | `{components?: [id]}` — download the pinned weights in the background (`experiment.install` events). |
| POST | `/renders/{render_id}/save` | `{project_id?, folder_id?, document_id?}` — store the render as a Library generation (with its mask and parameters). |
| POST | `/renders/{render_id}/export` | Write the render to a file → `{file_id, url, mime, name}` (the desktop saves it with `export_file`). |
| POST | `/mask/export` | `{mask, format: "index"\|"coco"\|"ade"\|"color"}` → PNG file and legend. |

## Events — `GET /v1/events` (WebSocket)

Messages are `{"type": "...", "ts": <unix>, "data": {...}}`. A `heartbeat` is sent
after 15 s of silence; clients reconnect if nothing arrives for ~40 s.

`hello, heartbeat, job.created, job.updated, job.progress {id, progress, text,
node_id, step, total}, job.preview {id, node_id, image (data URL)}, job.node {id,
node_id, status, elapsed_s?, message?}, generation.created, generation.updated,
generations.changed, download.created, download.updated, download.progress,
models.changed, llm.status, worker.status, worker.crashed, runtime.progress,
runtime.installed, runtime.failed, system.stats (every 2 s while subscribed),
projects.changed, settings.changed, experiment.status, experiment.progress {experiment,
render_id, session, step, total}, experiment.install, experiment.documents {experiment, document_id, action}`.
