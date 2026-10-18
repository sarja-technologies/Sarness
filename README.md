# Sarness (Sarja Harness)

A self-hosted AI workstation: **local LLM chat, image generation and editing,
text/image-to-video and node-based workflows** in one native Linux desktop app,
powered by your own GPU server.

* **Sarness Desktop** — Tauri 2 + Rust + Svelte 5 (no Electron, no React).
  Light-first graphite/off-white design with a single teal accent, optional
  dark theme, 120–240 ms motion.
* **Sarness Engine** — Python + FastAPI + PyTorch/Diffusers + llama.cpp, SQLite,
  a persistent VRAM-aware job queue, crash-isolated GPU worker and a typed DAG
  workflow executor. No ComfyUI.

```
desktop/   Tauri app (src/ = Svelte UI, src-tauri/ = Rust core: proxy, tunnels, keyring, installer)
engine/    Sarness Engine (sarness_engine/ package, tests/)
scripts/   install-engine.sh (server), setup-dev.sh, dev.sh, test.sh, build.sh, package-engine.sh
docs/      ARCHITECTURE.md · API.md · WORKFLOW_FORMAT.md · DEPLOYMENT.md · MODELS.md · DEVELOPMENT.md · EXPERIMENTS.md
dist/      build output (.deb, .rpm, .AppImage, engine bundle, SHA256SUMS)
```

## Quick start

Pre-built Linux installers (`.deb`, `.rpm`, `.AppImage`) and the engine bundle are on the
[Releases page](https://github.com/crossainthero-lab/Sarness/releases).

**Server** (Ubuntu, NVIDIA driver ≥ 570): either use the desktop's guided SSH
installer, or copy `dist/sarness-engine.tar.gz` + `scripts/install-engine.sh` and run

```bash
bash install-engine.sh --source sarness-engine.tar.gz --bind tailscale --with-llama
```

Note the API token it prints. **Desktop**: `sudo apt install ./Sarness_0.1.0_amd64.deb`
(or run the AppImage), launch *Sarness*, enter the server's Tailscale IP / MagicDNS
name / LAN IP and port `8742`, paste the token, **Test connection**, **Save & connect**.
Full guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Then open **Models → Catalog** and install what the "Recommended for this
server" badges suggest (for 12 GB GPUs: Qwen3 4B, SDXL Turbo/SDXL, Wan 2.1 1.3B,
Real-ESRGAN, SDXL fp16-fix VAE).

## Using Sarness

Sarness opens to **one conversation** with **one prompt box**, like a chat app. Pick **Chat, Code, Image or
Video** from the mode selector inside the prompt box; the model list and settings change to match, and
everything you make appears in the same timeline.

| Mode | What the prompt box offers | What appears in the conversation |
|---|---|---|
| **Chat** | Chat models from your server (local GGUF or remote endpoints), temperature/top-p/top-k/min-p/repeat/max-tokens, thinking, context and GPU layers, text and (for vision models) image attachments, Stop. | Streaming Markdown replies with copy, regenerate, edit and branch navigation. |
| **Code** | A **local coding agent**: choose a project folder (recent, open, or create new), a permission mode (Ask / Workspace Auto / Full Access) and a model. | The agent's plan, tool activity cards (files read, edits with diffs, commands with live terminal output), permission requests, and a final summary with changed files and real exit codes. Files are read, written and run **on the computer running Sarness**; the server only provides the model. See [docs/LOCAL_AGENT.md](docs/LOCAL_AGENT.md). |
| **Image** | Image models, aspect ratio, size, quality, seed, steps, guidance, scheduler, LoRAs, VAE, upscale, batch, memory mode, format; attach a reference image to edit it (strength, mask painting for inpaint). Only options the selected model supports are shown. | A progress card with live preview and **Cancel**, then the image with Edit prompt, Regenerate, Vary subtle/strong, same-seed rerun, favourite, save, export, open in folder. |
| **Video** | Video models, text→video or image→video (when the model supports it), resolution, length, frame rate, seed, steps, guidance, flow shift, an estimate measured from your server's own earlier runs. | A progress card with **Cancel**, then a playable video with the same actions. |

The chat model is told (as text) which images/videos were generated earlier in the conversation, so you
can refer to them; it cannot see the pixels. Both **Image** and **Video** have an optional **Open node
editor** (advanced) in their settings.

**Sidebar:** New conversation, search, projects, conversation history; plus Library (every image and
video with metadata, filters, trash), Models (installed, catalog, Hugging Face install), Server
(GPU/VRAM/RAM, the job queue with cancel/retry) and Settings. There are no separate Chat/Code/Image/Video
pages.

**Work | Experiments:** the switch at the top of the sidebar moves between your normal work and
**Experiments**, a space for interactive features (Alt+6). Entering Experiments shows the experiment
selection screen first. Experiments you have open keep their state while you are back in Work. The first
experiment is **God's Paintbrush**: paint a landscape with material brushes (sky, water, mountains, trees, …)
and the server turns it into a photorealistic landscape. A live GauGAN preview updates while you paint
(~70 ms on an RTX 4070 Ti), and a refined photo follows when you pause (~1.4 s). Paintings are saved on the
server, and results go to the Library. See [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

**Optional multi-conversation layout:** the layout button (top right) offers one, two (side by side or
stacked), three, or four (2×2) independent conversations, each with the same prompt box. Drag dividers to
resize, drag a header grip to swap, minimise/maximise/close. Default is one conversation. Layout and every
conversation (mode, draft, settings, generations) persist across restarts; jobs keep running on the server
while you work elsewhere.

Keyboard: `Ctrl+1…4` choose Chat/Code/Image/Video (←/→ also move between modes) · `Enter` send ·
`Shift+Enter` new line · `Esc` close settings · `Ctrl+Shift+O` new conversation · `Alt+1…5` sections · `Alt+6` Experiments ·
`Ctrl+Shift+1…4` focus a pane · `Ctrl+Shift+M` maximise/restore a pane · `Ctrl+B` sidebar.

## Where things are stored

* **Server (canonical):** `~/.local/share/sarness-engine/` — `outputs/images|videos/YYYY/MM/`,
  `sarness.db`, `models/`, `uploads/`, `thumbs/`, `logs/`.
* **Desktop:** `~/.config/sarness/profiles.json` (no secrets), tokens in the GNOME
  keyring, media cache `~/.cache/sarness/media` (6 GB LRU, viewable offline),
  “Save” copies to `~/Pictures/Sarness` and `~/Videos/Sarness`.

## Development

```bash
scripts/setup-dev.sh     # engine venv (CUDA torch) + pnpm install
scripts/dev.sh           # local engine + `tauri dev`
scripts/test.sh          # pytest, svelte-check, vitest, cargo test
scripts/build.sh         # dist/: .deb .rpm .AppImage + engine bundle
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Verified models, timings and VRAM measurements: [docs/MODELS.md](docs/MODELS.md).
