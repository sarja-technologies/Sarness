# Sarness (Sarja Harness)

Self-hosted AI workstation: an LLM chat/agent client, image and video generation, node-graph workflows and interactive experiments, driven from a native Linux desktop against one or more GPU hosts the operator controls (x86-64 or ARM64, including the NVIDIA DGX Spark; see [docs/MULTI-SERVER.md](docs/MULTI-SERVER.md)).

| Component | Version | Stack |
|---|---|---|
| Sarness Engine | 0.4.0 | Python ≥ 3.11, FastAPI, PyTorch/Diffusers, llama.cpp, SQLite, VRAM-aware persistent job queue, crash-isolated GPU worker, typed DAG workflow executor |
| Sarness Desktop | 0.2.0 | Tauri 2, Rust, Svelte 5; no Electron |
| HTTP API | 1.1 | JSON/SSE/WebSocket, [docs/API.md](docs/API.md) |
| SNP | 1 | Native binary protocol over TLS 1.3, [docs/SNP.md](docs/SNP.md) |

```
desktop/   src/ Svelte UI · src-tauri/ Rust core (loopback proxy, SNP client, SSH tunnels, keyring, installer, local agent)
engine/    sarness_engine/ package (api, chat, search, snp, jobs, models, runtime, workflow, experiments) · tests/
scripts/   install-engine.sh · package-engine.sh · setup-dev.sh · dev.sh · test.sh · build.sh
docs/      SNP.md · SNP-OPS.md · API.md · DEPLOYMENT.md · MULTI-SERVER.md · ARCHITECTURE.md · WORKFLOW_FORMAT.md · MODELS.md · EXPERIMENTS.md · LOCAL_AGENT.md · DEVELOPMENT.md · WINDOWS-WSL2.md
```

## Interfaces between the components

The desktop's webview communicates only with an authenticated loopback interface served by the desktop's native process; that process alone communicates with the engine, over one of three transports selected per connection profile, independently of whether an SSH forward carries the bytes:

| Transport | Engine port | Notes |
|---|---|---|
| `snp` | 8744 | One TLS 1.3 connection, multiplexed typed streams, flow control, idempotent retries, pinned server identity, HMAC challenge–response. No HTTP on the wire. |
| `http` | 8742 | JSON API, cleartext. |
| `https` | 8742 | JSON API over a CA-trusted certificate. |

A failure of any security check (pin, server proof, credentials) terminates the connection attempt; no transport falls back to another. Details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §6–§7, [docs/SNP.md](docs/SNP.md).

## Installation

Engine host requirements and procedures (guided SSH installation, manual installation, service units, configuration keys, firewall policy, upgrade and rollback): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Release artefacts (`.deb`, `.rpm`, `.AppImage`, `sarness-engine.tar.gz`, `SHA256SUMS`) are published on the releases page. Windows via WSL2 is unvalidated: [docs/WINDOWS-WSL2.md](docs/WINDOWS-WSL2.md).

```
bash install-engine.sh --source sarness-engine.tar.gz [--with-llama]      # engine host; prints the API token once
sudo apt install ./Sarness_<version>_amd64.deb                              # desktop
```

## Conversation surface

One conversation and one prompt box; the mode (Chat, Code, Image, Video) selects the model list and settings, and every artefact appears in the same timeline.

| Mode | Execution | Notes |
|---|---|---|
| Chat | Engine (local GGUF via llama.cpp, or OpenAI-compatible endpoints) | Streaming Markdown, branching (regenerate/edit create siblings), text and vision attachments, sampling parameters, `thinking`. **Reasoning display** and optional **web search** (below). |
| Code | Model on the engine; **tools execute on the desktop host**, confined to a chosen project directory | Ask / Workspace Auto / Full Access permission modes. [docs/LOCAL_AGENT.md](docs/LOCAL_AGENT.md). Never receives web search. |
| Image | Engine queue (local diffusers or gateway `gw:` models) | Options limited to model capabilities; txt2img, img2img, inpaint, LoRA, VAE, upscale, seeds, batch. |
| Video | Engine queue | t2v/i2v, resolution, frames, fps, flow shift, memory modes, timing estimates from prior local runs. |

**Reasoning (Chat).** While a model reasons, one muted line shows the latest segment of the reasoning it has actually emitted; selecting it expands the complete live trace. When the answer begins the trace folds (unless the reader opened it) into a *See thinking* control above the answer. Only provider-exposed reasoning (`reasoning_content`, `reasoning`) or a leading `<think>…</think>` block is used; models exposing none show only a neutral activity indicator. Reasoning is never synthesised, never reclassified from answer text and never fed back to the model. Semantics: [docs/API.md](docs/API.md#reasoning).

**Web search (Chat).** Off by default; enabled per message by the composer switch or globally by `search_policy`. The engine queries a provider (SearXNG, keyless DuckDuckGo, or Brave with a key), fetches pages under SSRF controls, supplies them to the model as delimited untrusted data, and returns numbered sources; answers cite `[n]` and only returned sources are linkable. Failure is reported, never masked. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §9, [docs/API.md](docs/API.md#web-search).

**Experiments** (Work | Experiments switch): interactive, GPU-resident features. *God's Paintbrush*: material-label painting rendered by a live GauGAN stage and a ControlNet-Seg refine stage. [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

Layouts of one to four independent conversations persist across restarts; jobs continue on the engine independently of the UI. Shortcuts: `Ctrl+1…4` mode, `Enter`/`Shift+Enter`, `Esc`, `Ctrl+Shift+O` new conversation, `Alt+1…6` sections, `Ctrl+Shift+1…4` focus pane, `Ctrl+Shift+M` maximise, `Ctrl+B` sidebar.

## State

* Engine: `<data_dir>` (default `~/.local/share/sarness-engine`): `sarness.db`, `config.json` (0600), `snp/` (0700), `models/`, `outputs/images|videos/YYYY/MM/`, `uploads/`, `thumbs/`, `logs/`.
* Desktop: `~/.config/sarness/profiles.json` (no secrets); API token in the Secret Service keyring (fallback `secrets.json`, 0600); content-addressed media cache `~/.cache/sarness/media` (6 GB LRU); exports to `~/Pictures/Sarness`, `~/Videos/Sarness`.

## Development

```
scripts/setup-dev.sh   scripts/dev.sh   scripts/test.sh   scripts/build.sh
```

Build, test and CI details: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md); structure: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); verified model measurements: [docs/MODELS.md](docs/MODELS.md).
