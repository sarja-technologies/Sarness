# Sarness architecture

Sarness is two programs that talk over a versioned HTTP/WebSocket API:

```
┌──────────────────────── Desktop (Tauri 2) ────────────────────────┐        ┌──────────────────── AI server ─────────────────────┐
│  Svelte 5 UI (webview)                                            │        │  Sarness Engine (Python, FastAPI, uvicorn)          │
│   views: Workspace · Images · Videos · Chat · Library · Models ·  │        │                                                     │
│          Settings · Advanced workflow editor (Svelte Flow)        │        │  API process (no torch import, never touches CUDA)  │
│        │ fetch / SSE / WebSocket to http://127.0.0.1:<port>       │        │   ├─ SQLite (WAL): jobs, generations, files,        │
│        ▼  + per-launch session key                                │ SNP /  │   │   projects, chats, workflows, models, downloads │
│  Rust core                                                        │ ─────► │   ├─ Job service: queue, admission control,        │
│   ├─ loopback reverse proxy (axum): injects the API token,        │  HTTP  │   │   OOM retry, crash recovery                     │
│   │  streams bodies, bridges /v1/events WebSocket,                │ direct │   ├─ Worker supervisor ──spawn──► GPU worker proc   │
│   │  content-addressed media cache (offline viewing)              │   or   │   │                              ├─ workflow exec  │
│   ├─ connection profiles + OS keyring tokens                      │  SSH   │   │                              ├─ model manager   │
│   ├─ SSH tunnels (ssh -L, key auth, auto-restart)                 │ tunnel │   │                              └─ diffusers/torch │
│   ├─ discovery (known peers + /v1/health probe)               │        │   ├─ llama.cpp manager ──spawn──► llama-server      │
│   └─ guided installer (scp + install-engine.sh over SSH)          │        │   ├─ downloads (resumable HF), model registry       │
└───────────────────────────────────────────────────────────────────┘        │   └─ event bus → /v1/events                         │
                                                                             └─────────────────────────────────────────────────────┘
```

## Desktop transports

The webview never knows which protocol is in use: it always talks to the authenticated
loopback proxy. A profile chooses the **transport** (how the desktop speaks to the
engine) and, independently, the **mode** (direct or through an SSH tunnel).

* **HTTP / HTTPS** - the proxy forwards requests to the engine's JSON API unchanged.
* **SNP** - the proxy does not contact the engine over HTTP at all. `snp/adapter.rs`
  maps each request to an SNP operation using the catalogue `engine/sarness_engine/snp/ops.json`
  (method + route + parameters), returns JSON in the shape the HTTP API would, turns
  `chat.generate` / `agent.completion` streams into the same `data: {json}` SSE bytes, bridges
  `/v1/events` to `event.subscribe`, serves `/v1/files/<id>` through `file.get` into the same
  content-addressed media cache, and streams uploads as `file.upload` DATA frames. A webview
  that disconnects cancels the SNP stream.
* The SNP client (`snp/client.rs`) keeps one TLS 1.3 connection with many streams, per-stream
  and connection flow control (credit is returned only after the consumer has consumed the
  data, so slow readers push back on the server), cancellation, deadlines, liveness pings,
  exponential-backoff reconnects and idempotency keys for retried writes. Identity or
  authentication failures are sticky: no automatic reconnect and never a fallback to HTTP.

See [SNP.md](SNP.md) for the protocol and [SNP-OPS.md](SNP-OPS.md) for the operation catalogue.

## Why these boundaries

* **The webview never sees the API token.** All engine traffic goes through the
  Rust loopback proxy, which adds `Authorization: Bearer …`. The webview only
  holds a random per-launch session key, so other local processes cannot use the
  proxy either. It also removes mixed-content/CORS issues for remote `http://`
  engines and lets SSH tunnels slot in transparently.
* **CUDA lives in a separate process.** The GPU worker is spawned with
  `multiprocessing` (`spawn`). A segfault, driver error or runaway allocation
  kills only the worker; the API keeps serving, the supervisor marks or retries
  the job and starts a fresh worker for the next one. When idle for the
  configured time the worker exits, which returns *all* VRAM (including the CUDA
  context) to the system.
* **llama.cpp runs as `llama-server`.** The engine downloads the official prebuilt
  CUDA (or Vulkan/CPU) release, starts one server per loaded model on a random
  loopback port and proxies its OpenAI-compatible streaming API. Remote
  OpenAI-compatible endpoints use the same code path.

## Jobs and GPU scheduling

1. `POST /v1/jobs` validates the workflow (types, required inputs, cycles, model
   categories/families, input files), resolves `seed = -1` to concrete seeds,
   derives a VRAM estimate and persists the job as `queued`.
2. The scheduler loop dispatches the oldest queued job when the worker is free.
   **Admission control** compares the estimate with free VRAM (plus VRAM the
   worker can reclaim from its own cache). If an *idle* chat model is in the way it
   is unloaded (configurable); if the chat model is streaming, the job waits and
   shows why.
3. The worker executes the graph; progress, latent previews and per-node states
   stream back over the pipe and out through `/v1/events`.
4. Outputs are written by the worker, then registered (SHA-256, thumbnail,
   generation row with full lineage metadata and the resolved workflow).
5. Failure handling:
   * **CUDA OOM** → the worker releases every model and clears caches; the job is
     re-queued once with `memory_mode=offload` (configurable), otherwise it fails
     with an actionable message attributed to the node.
   * **Worker crash** → job retried until `job_max_attempts`, then failed.
   * **Engine restart** → `running` jobs are re-queued (attempt counter incremented)
     or failed once attempts are exhausted; queued jobs simply continue;
     interrupted downloads become `paused` and resume from the partial file.
   * **Cancel** → cooperative (checked inside every sampler step); if the worker
     does not stop within 25 s it is killed and restarted.

## Experiments

Experiments are interactive features that need sub-second GPU round trips, which the job queue is not
designed for. Each experiment registers with `experiments/registry.py`. A GPU experiment gets its own worker
process (`experiments/worker.py`), spawned on first use and exited after an idle timeout, with the same crash
isolation as the job worker. Requests go through a latest-wins scheduler (`experiments/scheduler.py`): one
pending request per canvas session, and stale work is cancelled between steps.

Experiments always give way to the rest of Sarness:
* Dispatching a queued image/video job calls `experiments.yield_gpu()`. This cancels experiment work and stops
  the experiment's worker before the job's admission check.
* Loading a chat model does the same.
* While the AI gateway is running a GPU job, an idle experiment stands down.

Saved experiment documents live in the `experiment_documents` table. God's Paintbrush, its models and its
measurements are described in [EXPERIMENTS.md](EXPERIMENTS.md).

## Memory placement

| Model family | `gpu` | `offload` | `split` (video only) | `sequential` |
|---|---|---|---|---|
| SDXL / SD1.5 | all on GPU | diffusers model CPU offload | — | layer-by-layer |
| Wan | all on GPU | text encoder leaf-streamed **and** transformer blocks streamed | transformer + VAE on GPU, UMT5 text encoder leaf-streamed from RAM, tiled VAE decode | everything leaf-streamed |

`auto` picks the fastest placement whose estimate fits the *free* VRAM after
evicting other cached models. For LLMs, "all layers" uses llama.cpp's `--fit` so
a model that does not fully fit is partially offloaded instead of crashing.

## The workflow engine

* `workflow/types.py` — socket types, `Param`/`Socket`/`NodeSpec`.
* `workflow/registry.py` — node registry; node modules register on import and keep
  heavy imports (torch, diffusers) inside run functions so the API process can
  publish the node library and validate graphs without CUDA.
* `workflow/graph.py` — validation, Kahn topological order with pruning to
  ancestors of output nodes, seed resolution.
* `workflow/executor.py` — runs nodes in order; LRU cache keyed by
  `(type, params, upstream keys)` so changing only the seed re-runs only the
  sampler and its descendants; cancellation; error attribution with downstream
  nodes marked `skipped`; weighted progress; **lineage** — each node can annotate
  provenance (model, prompt, seed, LoRAs…) that flows to output nodes and into the
  saved metadata.
* `workflow/templates.py` — simple mode is not a separate code path: simple
  parameters compile into a real graph, and `to_simple()` maps a graph back only if
  it is *structurally identical* to what simple mode would build.

## Persistence

All canonical data lives on the server under the engine data directory
(`~/.local/share/sarness-engine` by default):

```
config.json        bind address, port, token hash, URL-signing key (0600)
sarness.db         SQLite database (WAL)
models/            llm/ image/ video/ upscaler/ vae/ lora/ controlnet/
                   experiments/<experiment>/<component>/  (not scanned as ordinary models)
outputs/images/YYYY/MM/sarness-…png     generated images (PNG embeds metadata)
outputs/videos/YYYY/MM/sarness-…mp4     generated videos
thumbs/            WebP thumbnails
uploads/           reference images, masks, chat attachments
bin/llama/         llama.cpp runtime
logs/              engine.log, worker.log, llama-server.log
```

The desktop keeps only connection profiles (`~/.config/sarness/profiles.json`),
tokens (system keyring; 0600 file fallback) and an LRU media cache
(`~/.cache/sarness/media`, 6 GB cap).
