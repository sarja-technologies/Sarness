# Developing Sarness

## Setup

```bash
scripts/setup-dev.sh          # engine/.venv with CUDA PyTorch + engine[inference,dev]; desktop pnpm install
```

Requirements: Python ≥ 3.11, Node 20+ with pnpm, Rust ≥ 1.80, Tauri's Linux
dependencies (`libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev
libayatana-appindicator3-dev build-essential pkg-config`), an NVIDIA GPU for
inference work.

## Running

```bash
scripts/dev.sh                # local engine on 127.0.0.1:8742 + `pnpm tauri dev`
```

On first run the engine prints an API token (`sarness-engine init`). Add a server
in the app with host `127.0.0.1`, port `8742` and that token.

UI-only work in a browser (no Tauri): `VITE_ENGINE_TOKEN=<token> scripts/dev.sh browser`
then open http://localhost:1420. In this mode the UI talks to the engine directly
(development only — never in packaged builds).

Engine only:

```bash
cd engine
.venv/bin/sarness-engine init --host 127.0.0.1
.venv/bin/sarness-engine serve -v
.venv/bin/sarness-engine doctor
```

## Tests

| Command | What it covers |
|---|---|
| `scripts/test.sh` | Everything below that needs no GPU. |
| `engine: .venv/bin/python -m pytest -m "not gpu"` | Graph validation (types, cycles, ranges, models), simple⇄graph round-trips, executor (order, caching, cancellation, error attribution, pruning, lineage), config/token handling, DB migrations, settings secret redaction, storage path safety, upload sniffing, signed URLs, model discovery (Diffusers, GGUF headers, safetensors, LoRA), API integration (auth, projects/folders incl. cycle refusal, workflows, jobs validation, uploads/files, chat CRUD/import/export, settings, catalog, WebSocket auth). |
| `desktop: npx vitest run` | Workflow graph conversion (groups, reroutes, defaults), connection validation and cycle checks, copy/paste cloning, chat branch tree, formatting and aspect maths. |
| `desktop: npx svelte-check` | Type checking of all Svelte/TypeScript code. |
| `desktop/src-tauri: cargo test` | Profile validation, SSH argument safety, proxy session checks, cache eviction, **proxy integration test** against a mock engine (session enforcement, token injection, SSE streaming, media cache with Range, WebSocket bridge, offline behaviour). |
| `engine/tests/gpu_smoke.py image|video|extra|all` | **Real inference** through the public API against a running engine with models installed (asserts outputs decode and have the expected size). |

## Adding a node type

1. Add a function in `engine/sarness_engine/workflow/nodes/*.py` decorated with
   `@node("category.name", title=…, category=…, inputs=[Socket(...)],
   outputs=[Socket(...)], params=[Param(...)], gpu=…)`.
2. Keep heavy imports (torch, diffusers) inside the function.
3. Use `ctx.progress(step, total, text, preview)` for progress (it also checks
   cancellation), `ctx.annotate(...)` for provenance, `ctx.services` for models,
   files and output paths. Raise `WorkflowError` for user-facing failures.
4. The node appears automatically in `GET /v1/nodes` and in the editor palette.

## Adding a model family / runtime adapter

* Diffusion pipelines: extend `runtime/models.py` (`image_pipeline` /
  `video_pipeline`) and the family maps in `models/registry.py`
  (`PIPELINE_FAMILIES`), then add a catalog entry in `models/catalog.py`
  with honest VRAM figures. Set capabilities/limits so the UI only shows supported
  controls.
* Chat models need nothing: any GGUF that llama.cpp supports works.

## Releasing

```bash
scripts/build.sh              # dist/: Sarness_<v>_amd64.deb, .rpm, .AppImage, sarness-engine.tar.gz, install-engine.sh, SHA256SUMS
```

Bump versions in `engine/pyproject.toml`, `engine/sarness_engine/__init__.py`,
`desktop/package.json`, `desktop/src-tauri/Cargo.toml` and
`desktop/src-tauri/tauri.conf.json`. Increase `API_VERSION`'s major version only
for breaking API changes (the desktop refuses mismatched majors).

## Which Sarness am I running?

Settings → About shows `Build <git sha>[+local] · <build time>`. If it does not match your checkout you are
running another copy. Copies that can exist side by side:

| Copy | Path | Notes |
|---|---|---|
| Installed package (`.deb`) | `/usr/bin/sarness`, launcher `/usr/share/applications/Sarness.desktop` | Whatever version was last installed with `apt`/`dpkg`. |
| Repo build | `desktop/src-tauri/target/release/sarness` | `cd desktop && pnpm tauri build --no-bundle`. |
| Release AppImage | `dist/Sarness_<version>_amd64.AppImage` | Only updated by `scripts/build.sh`. |

To always launch the repo build without touching the installed package, put a wrapper earlier on `PATH`
(for example `~/.local/bin/sarness` that `exec`s the repo binary) and a user-level
`~/.local/share/applications/Sarness.desktop` whose `Exec=` points at it; user launchers override
`/usr/share/applications`. Remove those two files to go back to the installed package.
