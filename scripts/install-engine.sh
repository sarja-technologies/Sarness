#!/usr/bin/env bash
# Sarness Engine installer.
#
#   * Installs entirely in user space (no sudo): ~/.local/share/sarness-engine-app
#   * Idempotent: re-running upgrades the code and keeps data, models and settings
#   * Never modifies unrelated services, firewalls, drivers or the OS
#   * Binds to the Tailscale IP, a LAN IP or loopback — never 0.0.0.0
#
# Usage:
#   install-engine.sh --source <engine.tar.gz | engine-dir> [--bind tailscale|loopback|lan]
#                     [--port 8742] [--with-llama] [--keep-token] [--json-result] [--no-service]
set -euo pipefail

SOURCE=""
BIND="tailscale"
PORT=8742
WITH_LLAMA=0
KEEP_TOKEN=0
JSON_RESULT=0
NO_SERVICE=0
PREFIX="${SARNESS_PREFIX:-$HOME/.local/share/sarness-engine-app}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --with-llama) WITH_LLAMA=1; shift ;;
    --keep-token) KEEP_TOKEN=1; shift ;;
    --json-result) JSON_RESULT=1; shift ;;
    --no-service) NO_SERVICE=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Sarness Engine supports Linux servers only."
[[ -n "$SOURCE" && -e "$SOURCE" ]] || die "--source must point to the engine archive or directory"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 0 && PORT < 65536 )) || die "invalid port $PORT"

# ---------------------------------------------------------------- python
PY=""
for cand in python3.13 python3.12 python3.11 python3.14 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done
[[ -n "$PY" ]] || die "Python 3.11+ is required. On Ubuntu: sudo apt install python3 python3-venv"
"$PY" -m venv --help >/dev/null 2>&1 || die "The Python venv module is missing. On Ubuntu: sudo apt install python3-venv"
say "Using $("$PY" --version) at $PY"

# ------------------------------------------------------------------ GPU
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) die "unsupported CPU architecture $ARCH (x86_64 and aarch64 are supported)" ;;
esac
say "CPU architecture: $ARCH"
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  MAJOR="${DRIVER%%.*}"
  # memory.total is [N/A] on unified-memory GPUs (DGX Spark GB10); the name is enough here.
  say "NVIDIA driver $DRIVER: $(nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader | head -1)"
  if [[ "$ARCH" == aarch64 ]]; then
    # ARM64 (DGX Spark / GB10, Grace Hopper): official CUDA 13 wheels carry Blackwell sm_120/sm_121a
    # kernels and need driver 580+; CUDA 12.8 aarch64 wheels are the fallback for older drivers.
    if (( MAJOR >= 580 )); then TORCH_INDEX="https://download.pytorch.org/whl/cu130"
    elif (( MAJOR >= 570 )); then TORCH_INDEX="https://download.pytorch.org/whl/cu128"
    else warn "Driver $DRIVER is too old for CUDA 12.8 on ARM64; installing CPU-only PyTorch."
    fi
  elif (( MAJOR >= 570 )); then TORCH_INDEX="https://download.pytorch.org/whl/cu128"
  elif (( MAJOR >= 560 )); then TORCH_INDEX="https://download.pytorch.org/whl/cu126"
  else warn "Driver $DRIVER is older than the CUDA 12.6 requirement; installing CPU-only PyTorch. Update the NVIDIA driver for GPU acceleration."
  fi
else
  warn "No working NVIDIA GPU detected; installing CPU-only PyTorch (generation will be very slow)."
fi

# ---------------------------------------------------------------- source
mkdir -p "$PREFIX"
STAGE="$PREFIX/src.new"
rm -rf "$STAGE"; mkdir -p "$STAGE"
if [[ -d "$SOURCE" ]]; then
  cp -a "$SOURCE" "$STAGE/engine"
else
  tar -xzf "$SOURCE" -C "$STAGE"
fi
[[ -f "$STAGE/engine/pyproject.toml" ]] || die "archive does not contain engine/pyproject.toml"
rm -rf "$PREFIX/src.old"
[[ -d "$PREFIX/src" ]] && mv "$PREFIX/src" "$PREFIX/src.old"
mv "$STAGE" "$PREFIX/src"
say "Engine source installed to $PREFIX/src"

# ------------------------------------------------------------------ venv
VENV="$PREFIX/venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  say "Creating virtual environment"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel
say "Installing PyTorch from $TORCH_INDEX (large download)"
"$VENV/bin/pip" install --progress-bar off torch torchvision --index-url "$TORCH_INDEX"
# Pin the PyTorch build just installed so the engine's dependencies cannot replace it with a
# different (e.g. CPU-only or wrong-architecture) wheel from PyPI.
CONSTRAINTS="$PREFIX/torch-constraints.txt"
"$VENV/bin/pip" freeze | grep -iE '^(torch|torchvision)==' > "$CONSTRAINTS" || true
say "Installing Sarness Engine and inference libraries"
"$VENV/bin/pip" install --progress-bar off "$PREFIX/src/engine[inference]" -c "$CONSTRAINTS" \
  --extra-index-url "$TORCH_INDEX"
ENGINE="$VENV/bin/sarness-engine"

# ------------------------------------------------------------------ bind
HOST="127.0.0.1"
case "$BIND" in
  tailscale)
    if command -v tailscale >/dev/null 2>&1 && TS_IP="$(tailscale ip -4 2>/dev/null | head -1)" && [[ -n "$TS_IP" ]]; then
      HOST="$TS_IP"
    else
      warn "No private-network interface (Tailscale) is running on this server; binding to 127.0.0.1 (connect with an SSH tunnel)."
    fi ;;
  lan)
    LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src") print $(i+1)}' | head -1)"
    if [[ -n "$LAN_IP" ]]; then HOST="$LAN_IP"; else warn "Could not determine the LAN address; using 127.0.0.1"; fi
    warn "LAN mode: make sure port $PORT is NOT forwarded on your router. Traffic is plain HTTP with a bearer token." ;;
  loopback) HOST="127.0.0.1" ;;
  *) die "invalid --bind $BIND" ;;
esac

# ------------------------------------------------------------------ init
INIT_ARGS=(init --host "$HOST" --port "$PORT" --json)
DATA_DIR="${SARNESS_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/sarness-engine}"
if [[ -f "$DATA_DIR/config.json" && $KEEP_TOKEN -eq 0 ]]; then
  INIT_ARGS+=(--rotate-token)
  say "Existing installation found; issuing a new API token for this desktop (other clients must re-pair)."
fi
INIT_JSON="$("$ENGINE" "${INIT_ARGS[@]}")"

if [[ $WITH_LLAMA -eq 1 ]]; then
  if [[ "$ARCH" != x86_64 ]]; then
    # No prebuilt ARM64 CUDA releases exist: the engine builds llama.cpp from source for this GPU.
    for tool in git cmake g++; do
      command -v "$tool" >/dev/null 2>&1 || warn "$tool is missing; the llama.cpp source build needs it (sudo apt install git cmake build-essential)"
    done
    say "Building the llama.cpp server runtime from source for $ARCH (10-20 minutes)"
  fi
  say "Installing the llama.cpp server runtime"
  "$ENGINE" install-llama || warn "llama.cpp runtime installation failed; you can retry from Sarness → Models."
fi

# --------------------------------------------------------------- service
SERVICE_MODE="manual"
if [[ $NO_SERVICE -eq 0 ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  # Cap the engine (GPU worker and llama-server included) below the machine's RAM so that, if a job runs out of
  # memory, the kernel stops it inside the engine instead of killing the desktop, sshd or other services. On
  # unified-memory systems (DGX Spark) GPU allocations are system RAM and count towards this limit.
  MEM_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  RESERVE_KB=$(( MEM_KB / 10 > 8388608 ? MEM_KB / 10 : 8388608 ))
  MEMORY_MAX="$(( (MEM_KB - RESERVE_KB) / 1024 ))M"
  cat > "$UNIT_DIR/sarness-engine.service" <<UNIT
[Unit]
Description=Sarness Engine (AI inference server)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$ENGINE serve
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
MemoryMax=$MEMORY_MAX
# The engine reports a GPU worker killed for memory as a failed job and starts a new worker itself.
OOMPolicy=continue
# Hardening that does not interfere with CUDA
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable sarness-engine.service >/dev/null 2>&1 || true
  systemctl --user restart sarness-engine.service
  SERVICE_MODE="systemd-user"
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" >/dev/null 2>&1 \
      && say "Enabled lingering so the engine runs without an active login session" \
      || warn "Could not enable lingering; the engine stops when you log out. Run: sudo loginctl enable-linger $USER"
  fi
  say "Installed systemd user service 'sarness-engine'"
else
  [[ $NO_SERVICE -eq 1 ]] || warn "systemd user services unavailable; starting the engine in the background."
  mkdir -p "$DATA_DIR/logs"
  nohup "$ENGINE" serve >"$DATA_DIR/logs/engine.nohup.log" 2>&1 < /dev/null &
  echo $! > "$DATA_DIR/engine.pid"
  SERVICE_MODE="background (pid $(cat "$DATA_DIR/engine.pid"))"
fi

# ----------------------------------------------------------------- health
say "Waiting for the engine to come up on $HOST:$PORT"
OK=0
for _ in $(seq 1 60); do
  if curl -fsS "http://$HOST:$PORT/v1/health" >/dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
[[ $OK -eq 1 ]] || die "engine did not become healthy; see $DATA_DIR/logs/engine.log"
say "Sarness Engine is running."

if [[ $JSON_RESULT -eq 1 ]]; then
  "$VENV/bin/python" - "$INIT_JSON" "$SERVICE_MODE" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1]); d["service"] = sys.argv[2]
print("SARNESS_RESULT " + json.dumps(d))
PYEOF
else
  echo "$INIT_JSON"
fi
