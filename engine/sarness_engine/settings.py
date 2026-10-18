"""Runtime settings persisted in SQLite, with typed defaults."""

from __future__ import annotations

import json
from typing import Any

from .db import Database

DEFAULTS: dict[str, Any] = {
    # Minutes an idle GPU worker keeps pipelines resident before exiting (frees all VRAM).
    "worker_idle_unload_minutes": 10,
    # Minutes an idle llama-server keeps its model loaded. 0 disables.
    "llm_idle_unload_minutes": 30,
    # If a generation job needs VRAM held by an idle LLM, unload the LLM automatically.
    "auto_unload_llm_for_generation": True,
    # Reserve this much VRAM as headroom when admitting jobs.
    "vram_headroom_mb": 600,
    # Extra directories scanned for models (read-only discovery).
    "extra_model_dirs": [],
    # Remote OpenAI-compatible chat providers: [{id, name, base_url, api_key?, models?}]
    "llm_endpoints": [],
    # Existing AI gateway on the server that owns the GPU (image/video run there).
    "gateway_url": "",
    "gateway_token": "",
    # Default llama-server options
    "llm_default_ctx": 8192,
    "llm_gpu_layers": 999,
    "llm_flash_attn": True,
    # Hugging Face access token (gated repos). Write-only from the API.
    "hf_token": "",
    # Retry a job once with CPU offload enabled after a CUDA OOM.
    "oom_retry_with_offload": True,
    # Maximum number of automatic restarts a single job may survive.
    "job_max_attempts": 2,
    # Experiments: minutes of inactivity before the large (refine) models are dropped from VRAM,
    # and before the experiment worker exits entirely (frees all of its VRAM). 0 disables.
    "experiments_refine_idle_minutes": 3,
    "experiments_worker_idle_minutes": 10,
}

SECRET_KEYS = {"hf_token", "gateway_token"}


class Settings:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", [key])
        if row is None:
            return DEFAULTS.get(key)
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value)],
        )

    def all_public(self) -> dict[str, Any]:
        out = {k: self.get(k) for k in DEFAULTS}
        for k in SECRET_KEYS:
            out[k] = bool(out[k])  # expose only whether a secret is set
        out["llm_endpoints"] = [
            {**e, "api_key": bool(e.get("api_key"))} for e in (out.get("llm_endpoints") or [])
        ]
        return out

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        for key, value in patch.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            if key in SECRET_KEYS and isinstance(value, bool):
                # Redacted secrets come back as booleans: True keeps the stored value, False clears it.
                if not value:
                    self.set(key, "")
                continue
            if key == "llm_endpoints":
                # Preserve stored keys when the client sends back the redacted boolean.
                existing = {e.get("id"): e for e in (self.get("llm_endpoints") or [])}
                merged = []
                for e in value:
                    e = dict(e)
                    if e.get("api_key") in (True, None) and e.get("id") in existing:
                        e["api_key"] = existing[e["id"]].get("api_key", "")
                    elif e.get("api_key") is False:
                        e["api_key"] = ""
                    merged.append(e)
                value = merged
            expected = type(DEFAULTS[key])
            if expected is int and isinstance(value, float):
                value = int(value)
            if not isinstance(value, expected) and not (expected is float and isinstance(value, int)):
                raise TypeError(f"{key} must be {expected.__name__}")
            self.set(key, value)
        return self.all_public()
