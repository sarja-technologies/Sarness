"""Shared API dependencies: application state and authentication."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, WebSocket


@dataclass
class AppState:
    cfg: Any
    db: Any
    bus: Any
    settings: Any
    storage: Any
    registry: Any
    downloads: Any
    models: Any
    llama_runtime: Any
    llama: Any
    jobs: Any
    chat: Any
    gateway: Any
    started_at: float
    experiments: Any = None
    experiment_docs: Any = None


def state(request: Request) -> AppState:
    return request.app.state.sarness


_failures: dict[str, deque] = defaultdict(deque)
MAX_FAILURES_PER_MINUTE = 20


def _client_ip(conn: Request | WebSocket) -> str:
    return conn.client.host if conn.client else "?"


def _note_failure(ip: str) -> None:
    q = _failures[ip]
    q.append(time.time())
    while q and time.time() - q[0] > 60:
        q.popleft()


def _locked(ip: str) -> bool:
    q = _failures.get(ip)
    if not q:
        return False
    while q and time.time() - q[0] > 60:
        q.popleft()
    return len(q) >= MAX_FAILURES_PER_MINUTE


def extract_token(conn: Request | WebSocket) -> str | None:
    auth = conn.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return conn.query_params.get("token")


def require_auth(request: Request) -> AppState:
    st: AppState = request.app.state.sarness
    ip = _client_ip(request)
    if _locked(ip):
        raise HTTPException(429, "too many failed authentication attempts; try again in a minute")
    if not st.cfg.check_token(extract_token(request)):
        _note_failure(ip)
        raise HTTPException(401, "invalid or missing API token", headers={"WWW-Authenticate": "Bearer"})
    return st


def check_ws(ws: WebSocket) -> bool:
    st: AppState = ws.app.state.sarness
    ip = _client_ip(ws)
    if _locked(ip):
        return False
    ok = st.cfg.check_token(extract_token(ws))
    if not ok:
        _note_failure(ip)
    return ok
