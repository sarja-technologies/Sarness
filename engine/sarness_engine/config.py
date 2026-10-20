"""Engine bootstrap configuration.

Bootstrap settings (bind address, port, data directory, auth token hash) live in
``<data_dir>/config.json`` with 0600 permissions. Runtime-tunable preferences live
in the SQLite ``settings`` table (see :mod:`sarness_engine.settings`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PORT = 8742
DEFAULT_SNP_PORT = 8744


def default_data_dir() -> Path:
    env = os.environ.get("SARNESS_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "sarness-engine"


@dataclass
class EngineConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token_sha256: str = ""
    # Secret used to sign short-lived file URLs. Never leaves the server.
    signing_key: str = field(default_factory=lambda: secrets.token_hex(32))
    # Extra CORS origins (the desktop normally talks through its loopback proxy).
    cors_origins: list[str] = field(default_factory=list)
    data_dir: str = ""
    # SNP/1 native protocol (docs/SNP.md). snp_host None means "same as host".
    snp_enabled: bool = True
    snp_host: str | None = None
    snp_port: int = DEFAULT_SNP_PORT
    # Extra DNS names / IP addresses for the SNP certificate (the pin does not change with them).
    snp_names: list[str] = field(default_factory=list)
    # Stable identity of this installation, so a multi-server desktop can tell engines apart
    # even when addresses change. Created on first start; survives upgrades.
    server_id: str = ""

    # ---- derived paths -------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.root / "sarness.db"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def ensure_dirs(self) -> None:
        for p in (self.root, self.models_dir, self.outputs_dir, self.uploads_dir,
                  self.thumbs_dir, self.bin_dir, self.logs_dir, self.tmp_dir):
            p.mkdir(parents=True, exist_ok=True)
        for cat in ("llm", "image", "video", "upscaler", "vae", "lora", "controlnet"):
            (self.models_dir / cat).mkdir(exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    # ---- auth ------------------------------------------------------------
    def check_token(self, token: str | None) -> bool:
        if not token or not self.token_sha256:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(digest, self.token_sha256)

    def rotate_token(self) -> str:
        token = "sk-sarness-" + secrets.token_urlsafe(32)
        self.token_sha256 = hashlib.sha256(token.encode()).hexdigest()
        return token


def config_path(data_dir: Path) -> Path:
    return data_dir / "config.json"


def load_config(data_dir: Path | None = None) -> EngineConfig:
    data_dir = (data_dir or default_data_dir()).expanduser().resolve()
    path = config_path(data_dir)
    cfg = EngineConfig(data_dir=str(data_dir))
    if path.exists():
        raw = json.loads(path.read_text())
        for k, v in raw.items():
            if hasattr(cfg, k) and k != "data_dir":
                setattr(cfg, k, v)
    # Environment overrides for containers / systemd units.
    if os.environ.get("SARNESS_HOST"):
        cfg.host = os.environ["SARNESS_HOST"]
    if os.environ.get("SARNESS_PORT"):
        cfg.port = int(os.environ["SARNESS_PORT"])
    if os.environ.get("SARNESS_SNP_HOST"):
        cfg.snp_host = os.environ["SARNESS_SNP_HOST"]
    if os.environ.get("SARNESS_SNP_PORT"):
        cfg.snp_port = int(os.environ["SARNESS_SNP_PORT"])
    return cfg


def save_config(cfg: EngineConfig) -> None:
    cfg.root.mkdir(parents=True, exist_ok=True)
    path = config_path(cfg.root)
    data = asdict(cfg)
    data.pop("data_dir", None)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
