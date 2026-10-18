# Deploying Sarness

Sarness has two parts:

* **Sarness Engine** — runs on the AI server (Ubuntu Server with an NVIDIA GPU, e.g.
  RTX 4070 Ti 12 GB, 32 GB RAM).
* **Sarness Desktop** — runs on your Ubuntu GNOME desktop, laptop or any trusted
  Linux computer.

Nothing needs to be exposed to the internet and no router ports are opened.

## 1. Server prerequisites

* Ubuntu 22.04+ (tested on Ubuntu 26.04), x86-64.
* NVIDIA driver **≥ 570** (CUDA 12.8 wheels; ≥ 560 falls back to CUDA 12.6 wheels).
  Check with `nvidia-smi`. Sarness never installs or changes drivers.
* Python 3.11+ with `venv` (`sudo apt install python3 python3-venv` if missing).
* ~15 GB free for the runtime (PyTorch, llama.cpp) plus space for models
  (SDXL ≈ 7 GB, Wan 2.1 1.3B ≈ 29 GB, Wan 2.2 5B ≈ 34 GB, chat models 2–8 GB).
* For Wan video on a 12 GB GPU: **≥ 24 GB system RAM** (the 11 GB UMT5 text
  encoder is kept in RAM and streamed to the GPU).
* A way for the desktop to reach the server: the same network, a VPN, or key-based SSH.

## 2a. Guided install from the desktop (SSH)

Requirements: key-based SSH access to the server (`ssh user@server` works without a
password prompt, e.g. via `ssh-copy-id` and ssh-agent). Sarness never asks for or
stores SSH passwords.

1. Open Sarness → **Settings → Servers → Add server** (or the first-run screen).
2. Turn on **Connect through SSH tunnel**, fill in SSH user/host (a host name or IP
   address), then **Install engine via SSH…**.
3. Choose where the engine listens:
   * **Private network (recommended)** — binds to the server's private-network
     interface only (when none is available it falls back to localhost).
   * **Localhost + SSH tunnel** — binds to 127.0.0.1; the desktop connects through
     an automatically maintained `ssh -L` tunnel.
   * **Local network address** — binds to the server's primary LAN IP (plain HTTP
     with a bearer token; only use on a trusted network).
4. The installer copies the engine bundled in the app, installs it in user space,
   creates a systemd *user* service, starts it, waits for `/v1/health`, issues an
   API token and stores it straight into your desktop keyring.

What the installer does **not** do: use sudo, touch other services, change
firewall rules, install drivers, or delete anything outside its own directories.
Re-running it upgrades in place and keeps models, generations and settings
(it issues a new token for the desktop that ran it).

## 2b. Manual install on the server

Copy `dist/sarness-engine.tar.gz` (built by `scripts/package-engine.sh`, or the
`engine/` folder) and `scripts/install-engine.sh` to the server, then:

```bash
bash install-engine.sh --source sarness-engine.tar.gz --with-llama
```

The last line printed is a JSON object containing the **API token** (shown once).
Options: `--bind tailscale|loopback|lan` (`tailscale`, the default, means the
server's private-network interface), `--port 8742`, `--with-llama`,
`--keep-token` (don't rotate on reinstall), `--no-service` (start in the
background instead of creating a systemd user service).

Fully manual alternative:

```bash
python3 -m venv ~/sarness-venv
~/sarness-venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
~/sarness-venv/bin/pip install "./engine[inference]"
~/sarness-venv/bin/sarness-engine init --host SERVER_ADDRESS   # prints the token
~/sarness-venv/bin/sarness-engine install-llama
~/sarness-venv/bin/sarness-engine serve
```

### Service management

```bash
systemctl --user status sarness-engine
systemctl --user restart sarness-engine
journalctl --user -u sarness-engine -f          # or ~/.local/share/sarness-engine/logs/engine.log
sudo loginctl enable-linger "$USER"              # keep running without an active login (the installer tries this)
~/.local/share/sarness-engine-app/venv/bin/sarness-engine doctor      # diagnostics
~/.local/share/sarness-engine-app/venv/bin/sarness-engine token --rotate   # new token
```

## 3. Connecting the desktop

Install the desktop package:

```bash
sudo apt install ./Sarness_0.1.0_amd64.deb      # or: chmod +x Sarness_0.1.0_amd64.AppImage && ./Sarness_0.1.0_amd64.AppImage
```

First launch shows **Connect to your server**:

| Connection | Server address | Notes |
|---|---|---|
| Direct | `192.168.1.50` or `ai-server.example.net` | Engine bound to that interface (`--bind lan`, or the private-network default). |
| SSH tunnel | Engine address `127.0.0.1`, SSH user/host | Engine bound to loopback; the desktop runs and supervises `ssh -N -L`. |
| This computer | `127.0.0.1` | Local testing. |

Pick the **Protocol** (see [3a](#3a-choosing-a-protocol)) and use **Find servers → Scan**
to look for engines on this computer and on devices the system already knows about.
Paste the API token, press **Test connection** — you will see the GPU, VRAM,
RAM, CPU, storage, CUDA/runtime versions, model count, queue and health — then
**Save & connect**. The token is stored in the GNOME keyring (Secret Service);
if no keyring is available it falls back to `~/.config/sarness/secrets.json`
with 0600 permissions. Tokens never appear in logs or in the webview.

## 3a. Choosing a protocol

The desktop can talk to the engine in three ways; the choice is independent of
whether the connection goes through an SSH tunnel.

| Protocol | Port (default) | What it is |
|---|---|---|
| **SNP** | 8744 | Native binary protocol ([SNP.md](SNP.md)): one TLS 1.3 connection, challenge-response authentication bound to the server identity, multiplexed streams, flow control. |
| **HTTP** | 8742 | The JSON API, unencrypted. Fine on a trusted network or inside an SSH tunnel. |
| **HTTPS** | 8742 | The JSON API behind a certificate your system trusts. |

**SNP server identity.** The engine creates a self-signed certificate on first
start. Its fingerprint is the SHA-256 of the public key (`sha256:…`). In the server
form press **Fetch identity**: the desktop connects once, only to read the
fingerprint (it sends no credentials), shows it, and stores it only after you
confirm that it matches the server. From then on a different identity is refused
with an error that shows the expected and the presented fingerprint; it is never
accepted automatically and the desktop never falls back to HTTP. If the server
uses a certificate from a certificate authority, leave the identity empty and enter
the **Server name** on the certificate instead. A wrong token, a failed server
proof or a changed identity stop reconnection until you fix the profile and
reconnect yourself.

**Through an SSH tunnel** the tunnel forwards the SNP port (or the API port for
HTTP/HTTPS); TLS and authentication still run end-to-end with the server. For a
tunnelled SNP connection the engine address is usually `127.0.0.1` on the server.

Existing servers keep working: profiles saved by earlier versions open with the
HTTP or HTTPS protocol they were using.

## 3b. Servers that already run an AI gateway

If the server already has its own AI service that owns the GPU (an HTTP gateway with
`/models`, `/uploads`, `/jobs/image`, `/jobs/video` and an OpenAI-compatible `/v1`), do
not install a second copy of its models. In **Settings → Engine → Server AI gateway** enter
its URL as the engine sees it (e.g. `http://127.0.0.1:8080`) and its API token. Its image
and video models then appear as *Server gateway* models (`gw:<id>`): jobs still go through
the Sarness queue, progress, cancel and library, but inference runs in the gateway. For chat,
add the gateway's `/v1` under **Settings → Remote chat endpoints** with the same token.

* The token is stored on the engine as a secret and is never returned by the API or logged.
* Gateway models run the standard simple-mode graph only; custom node graphs need local models.
* Before a gateway job, Sarness frees anything it holds on the GPU itself (idle chat model, idle worker).

## 4. Security notes

* The engine requires a bearer token on every endpoint except a minimal health
  check, rate-limits failed authentication, and never logs query strings.
* Binding defaults to the private-network interface or loopback, never `0.0.0.0`
  (you can force it with `--host 0.0.0.0`; the engine prints a warning).
* Uploaded files are content-sniffed and verified; file paths are resolved
  inside the data directory only; models are deleted only from the managed
  models directory.
* Models are loaded with `safetensors`/GGUF; Sarness does not enable
  `trust_remote_code`. Upscalers may be `.pth` files (loaded by spandrel, which
  restricts unpickling to tensors) — only install upscalers from sources you trust.
* For LAN mode, keep the port closed on your router; traffic is plain HTTP.
  Prefer SNP, HTTPS or an SSH tunnel for anything beyond a trusted home network.

## 5. Upgrades and backups

* Upgrade: re-run the guided installer (or `install-engine.sh` with
  `--keep-token`), then update the desktop package.
* Backup: `~/.local/share/sarness-engine/` (database, outputs, uploads, settings).
  Models can be re-downloaded. Stop the service before copying `sarness.db`.

## Discovery diagnostics and private networks

**Find servers → Scan** reads the devices the system already knows about and probes only
the engine's health endpoint on them (and on this computer); it never scans ports. When
Tailscale is installed on the desktop, its online peers are included (`tailscale status
--json`), their MagicDNS names (`host.tailnet.ts.net`) and `100.x.y.z` addresses work as
the server address like any other host name or IP, and `--bind tailscale` binds the
engine to the server's Tailscale address. If a Tailscale peer is not listed:

* check that `tailscale status` works in a terminal on the desktop and the server is online;
* use the peer's MagicDNS name or its `100.x.y.z` address as the server address;
* for SNP, fetch the identity again if the server was reinstalled.
