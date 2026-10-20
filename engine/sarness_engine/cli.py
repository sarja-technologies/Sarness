"""``sarness-engine`` command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import EngineConfig, default_data_dir, load_config, save_config


def _tailscale_ip() -> str | None:
    if not shutil.which("tailscale"):
        return None
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
        return ip
    except Exception:
        return None


def cmd_init(args) -> int:
    data = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    cfg = load_config(data)
    fresh = not cfg.token_sha256
    if args.host:
        cfg.host = args.host
    elif fresh:
        # Secure default: Tailscale interface if present, else loopback only (use SSH forwarding).
        cfg.host = _tailscale_ip() or "127.0.0.1"
    if args.port:
        cfg.port = args.port
    token = None
    if fresh or args.rotate_token:
        token = cfg.rotate_token()
    cfg.ensure_dirs()
    save_config(cfg)
    result = {"data_dir": str(cfg.root), "host": cfg.host, "port": cfg.port, "token": token}
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Sarness Engine configured in {cfg.root}")
        print(f"  listen: {cfg.host}:{cfg.port}")
        if token:
            print(f"  API token (shown once — paste it into Sarness Desktop): {token}")
        else:
            print("  API token unchanged (use 'sarness-engine token --rotate' for a new one)")
    return 0


def cmd_token(args) -> int:
    cfg = load_config(Path(args.data_dir).expanduser() if args.data_dir else None)
    if not args.rotate:
        print("Tokens are stored hashed and cannot be displayed. Use --rotate to issue a new token.")
        return 1
    token = cfg.rotate_token()
    save_config(cfg)
    print(json.dumps({"token": token}) if args.json else f"New API token: {token}")
    return 0


def _setup_logging(cfg: EngineConfig, verbose: bool) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(cfg.logs_dir / "engine.log", maxBytes=10 * 2**20, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app

    data = Path(args.data_dir).expanduser() if args.data_dir else None
    cfg = load_config(data)
    if not cfg.token_sha256:
        print("Engine is not initialised. Run 'sarness-engine init' first.", file=sys.stderr)
        return 2
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if cfg.host in ("0.0.0.0", "::"):
        print("WARNING: listening on all interfaces. Make sure your firewall blocks the port from the internet.",
              file=sys.stderr)
    _setup_logging(cfg, args.verbose)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None, access_log=False,
                ws_ping_interval=20, ws_ping_timeout=30, timeout_keep_alive=30)
    return 0


def cmd_snp_identity(args) -> int:
    from .snp.identity import load_or_create

    cfg = load_config(Path(args.data_dir).expanduser() if args.data_dir else None)
    cfg.ensure_dirs()
    host = cfg.snp_host or cfg.host
    ident = load_or_create(cfg.root, host, [cfg.host, *cfg.snp_names])
    info = {"pin": ident.pin, "cert": str(ident.cert_path), "listen": f"{host}:{cfg.snp_port}", "enabled": cfg.snp_enabled}
    if args.json:
        print(json.dumps(info))
    else:
        print("SNP/1 server identity (enter this pin in Sarness Desktop)")
        print(f"  pin     : {info['pin']}")
        print(f"  cert    : {info['cert']}")
        print(f"  listen  : {info['listen']}{'' if cfg.snp_enabled else '  (disabled in config)'}")
    return 0


def cmd_install_llama(args) -> int:
    from .runtime.llama import LlamaRuntime

    cfg = load_config(Path(args.data_dir).expanduser() if args.data_dir else None)
    rt = LlamaRuntime(cfg.bin_dir)
    last = [-1]

    def progress(done, total):
        if total < 100:  # source build: steps, not bytes
            print(f"  step {done}/{total}", flush=True)
            return
        pct = int(done * 100 / total) if total else 0
        if pct != last[0] and pct % 5 == 0:
            last[0] = pct
            print(f"  {pct}% ({done / 1e6:.0f}/{total / 1e6:.0f} MB)", flush=True)

    print(f"Installing llama.cpp ({args.variant or rt.choose_variant()})…")
    version = rt.install(progress, args.variant, getattr(args, "ref", None))
    print(f"Installed llama.cpp {version} at {rt.binary()}")
    return 0


def cmd_doctor(args) -> int:
    from . import hardware

    cfg = load_config(Path(args.data_dir).expanduser() if args.data_dir else None)
    snap = hardware.system_snapshot(cfg.root if cfg.root.exists() else Path.home())
    print(f"Sarness Engine {__version__}")
    print(f"  data dir : {cfg.root}  (initialised: {bool(cfg.token_sha256)})")
    print(f"  listen   : {cfg.host}:{cfg.port}")
    print(f"  CPU      : {snap['cpu']['name']} ({snap['cpu']['cores_logical']} threads)")
    print(f"  RAM      : {snap['memory']['total_mb'] / 1024:.1f} GiB")
    for g in snap["gpus"]:
        print(f"  GPU {g['index']}    : {g['name']} {g['vram_total_mb']} MiB (driver {g['driver_version']}, CUDA {g['cuda_driver_version']})")
    if not snap["gpus"]:
        print("  GPU      : none detected (NVIDIA driver / NVML unavailable)")
    try:
        import torch

        print(f"  torch    : {torch.__version__} CUDA available={torch.cuda.is_available()}")
    except ImportError:
        print("  torch    : NOT INSTALLED")
    from .runtime.llama import LlamaRuntime

    rt = LlamaRuntime(cfg.bin_dir)
    print(f"  llama.cpp: {rt.version() or 'not installed'}")
    print(f"  storage  : {snap['storage']['free_gb']} GB free")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sarness-engine", description="Sarness Engine server")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data-dir", help="data directory (default: $SARNESS_HOME or ~/.local/share/sarness-engine)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create configuration and API token")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--rotate-token", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the API server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("token", help="issue a new API token")
    s.add_argument("--rotate", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_token)

    s = sub.add_parser("snp-identity", help="print the SNP/1 certificate pin (never key material)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_snp_identity)

    s = sub.add_parser("install-llama", help="download the llama.cpp server runtime")
    s.add_argument("--variant", choices=["cuda-12.8", "vulkan", "cpu", "cuda-src", "cpu-src"],
                   help="prebuilt x86-64 release, or *-src to build from source (default: auto)")
    s.add_argument("--ref", help="llama.cpp release tag for source builds (default: latest)")
    s.set_defaults(fn=cmd_install_llama)

    s = sub.add_parser("doctor", help="print environment diagnostics")
    s.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
