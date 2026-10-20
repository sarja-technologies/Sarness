"""Resumable Hugging Face downloads with disk-space validation and progress events."""

from __future__ import annotations

import fnmatch
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..db import Database, new_id, now
from ..hardware import disk_free_bytes

log = logging.getLogger("sarness.downloads")

HF = "https://huggingface.co"
CHUNK = 1 << 20
SAFETY_MARGIN = 2 * 1024**3  # keep 2 GiB free after a download


class DownloadError(Exception):
    pass


def _client(token: str | None) -> httpx.Client:
    headers = {"User-Agent": "sarness-engine/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))


def list_repo_files(repo: str, token: str | None = None, revision: str = "main") -> list[dict[str, Any]]:
    """Return ``[{path, size}]`` for every file in a model repo."""
    with _client(token) as c:
        r = c.get(f"{HF}/api/models/{repo}/tree/{quote(revision, safe='')}", params={"recursive": "true"})
        if r.status_code == 401 or r.status_code == 403:
            raise DownloadError("this repository is gated or private; add a Hugging Face token in Settings")
        if r.status_code == 404:
            raise DownloadError(f"repository {repo} was not found")
        r.raise_for_status()
        items = r.json()
    return [{"path": i["path"], "size": i.get("lfs", {}).get("size") or i.get("size") or 0}
            for i in items if i.get("type") == "file"]


def select_files(files: list[dict[str, Any]], patterns: list[str] | None) -> list[dict[str, Any]]:
    if not patterns:
        return [f for f in files if not f["path"].startswith((".git", "assets/", "examples/"))]
    out = []
    for f in files:
        if any(fnmatch.fnmatch(f["path"], p) for p in patterns):
            out.append(f)
    return out


class DownloadManager:
    def __init__(self, db: Database, publish: Callable[[str, dict], None], token_getter: Callable[[], str],
                 on_complete: Callable[[dict[str, Any]], None], max_parallel: int = 2):
        self.db = db
        self.publish = publish
        self.token_getter = token_getter
        self.on_complete = on_complete
        self.max_parallel = max_parallel
        self._threads: dict[str, threading.Thread] = {}
        self._stop: dict[str, threading.Event] = {}
        self._speed: dict[str, float] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- API
    def recover(self, resume: bool = True) -> None:
        """After a restart, downloads that were running or waiting continue on their own, oldest first
        (finished files are kept and partial ones resume). With resume=False they are paused instead.
        Downloads the user paused stay paused."""
        if resume:
            self.db.execute("UPDATE downloads SET status = 'queued', updated_at = ? WHERE status = 'downloading'", [now()])
            self._pump()
        else:
            self.db.execute("UPDATE downloads SET status = 'paused', updated_at = ? WHERE status IN ('downloading', 'queued')",
                            [now()])

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM downloads ORDER BY created_at DESC LIMIT 200")
        for r in rows:
            r["speed_bps"] = self._speed.get(r["id"], 0.0)
        return rows

    def get(self, did: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM downloads WHERE id = ?", [did])

    def create(self, *, model_id: str, name: str, category: str, repo: str, patterns: list[str] | None,
               dest: Path, catalog_id: str | None = None, revision: str = "main",
               meta: dict[str, Any] | None = None) -> dict[str, Any]:
        active = self.db.one("SELECT * FROM downloads WHERE model_id = ? AND status IN ('queued','downloading','paused')",
                             [model_id])
        if active:
            return active
        files = select_files(list_repo_files(repo, self.token_getter() or None, revision), patterns)
        if not files:
            raise DownloadError("no matching files in repository")
        total = sum(f["size"] for f in files)
        already = sum((dest / f["path"]).stat().st_size for f in files if (dest / f["path"]).exists())
        needed = total - already
        free = disk_free_bytes(dest)
        if needed + SAFETY_MARGIN > free:
            raise DownloadError(f"not enough disk space: need {needed / 1e9:.1f} GB plus 2 GB headroom, "
                                f"{free / 1e9:.1f} GB free")
        row = {
            "id": new_id("d_"), "model_id": model_id, "catalog_id": catalog_id, "name": name, "category": category,
            "repo": repo, "revision": revision, "files": files, "dest": str(dest), "total_bytes": total,
            "done_bytes": already, "status": "queued", "error": None, "meta": meta or {},
            "created_at": now(), "updated_at": now(),
        }
        self.db.insert("downloads", row)
        self.publish("download.created", row)
        self._pump()
        return row

    def pause(self, did: str) -> None:
        ev = self._stop.get(did)
        if ev:
            ev.set()
        self.db.execute("UPDATE downloads SET status = 'paused', updated_at = ? WHERE id = ? AND status IN ('queued','downloading')",
                        [now(), did])
        self.publish("download.updated", self.get(did) or {"id": did})

    def resume(self, did: str) -> None:
        self.db.execute("UPDATE downloads SET status = 'queued', error = NULL, updated_at = ? WHERE id = ? AND status IN ('paused','failed')",
                        [now(), did])
        self.publish("download.updated", self.get(did) or {"id": did})
        self._pump()

    def cancel(self, did: str) -> None:
        ev = self._stop.get(did)
        if ev:
            ev.set()
        t = self._threads.get(did)
        if t:
            t.join(10)
        row = self.get(did)
        if row:
            dest = Path(row["dest"])
            for f in row["files"]:
                part = dest / (f["path"] + ".partial")
                part.unlink(missing_ok=True)
            self.db.execute("UPDATE downloads SET status = 'cancelled', updated_at = ? WHERE id = ?", [now(), did])
            # Remove the directory if nothing completed inside it.
            if dest.exists() and dest.is_dir() and not any(p.is_file() for p in dest.rglob("*")):
                shutil.rmtree(dest, ignore_errors=True)
        self.publish("download.updated", self.get(did) or {"id": did})

    # ---------------------------------------------------------------- work
    def _pump(self) -> None:
        with self._lock:
            running = [d for d, t in self._threads.items() if t.is_alive()]
            slots = self.max_parallel - len(running)
            if slots <= 0:
                return
            for row in self.db.all("SELECT id FROM downloads WHERE status = 'queued' ORDER BY created_at LIMIT ?", [slots]):
                did = row["id"]
                if did in running:
                    continue
                ev = threading.Event()
                self._stop[did] = ev
                t = threading.Thread(target=self._run, args=(did, ev), daemon=True, name=f"dl-{did}")
                self._threads[did] = t
                self.db.execute("UPDATE downloads SET status = 'downloading', updated_at = ? WHERE id = ?", [now(), did])
                t.start()

    def _run(self, did: str, stop: threading.Event) -> None:
        row = self.get(did)
        if not row:
            return
        dest = Path(row["dest"])
        dest.mkdir(parents=True, exist_ok=True)
        done = 0
        last_emit = 0.0
        window: list[tuple[float, int]] = []
        try:
            with _client(self.token_getter() or None) as client:
                for f in row["files"]:
                    target = dest / f["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and (not f["size"] or target.stat().st_size == f["size"]):
                        done += target.stat().st_size
                        continue
                    part = target.with_name(target.name + ".partial")
                    offset = part.stat().st_size if part.exists() else 0
                    url = f"{HF}/{row['repo']}/resolve/{quote(row['revision'] or 'main', safe='')}/{quote(f['path'])}"
                    headers = {"Range": f"bytes={offset}-"} if offset else {}
                    with client.stream("GET", url, headers=headers) as r:
                        if r.status_code in (401, 403):
                            raise DownloadError("access denied (gated model?) — add a Hugging Face token in Settings")
                        if r.status_code == 416:
                            offset = 0
                        elif r.status_code == 200 and offset:
                            offset = 0  # server ignored Range; restart this file
                        elif r.status_code not in (200, 206):
                            raise DownloadError(f"HTTP {r.status_code} for {f['path']}")
                        mode = "ab" if offset else "wb"
                        with open(part, mode) as out:
                            file_done = offset
                            for chunk in r.iter_bytes(CHUNK):
                                if stop.is_set():
                                    raise _Paused()
                                out.write(chunk)
                                file_done += len(chunk)
                                t = time.monotonic()
                                window.append((t, len(chunk)))
                                if t - last_emit > 0.5:
                                    last_emit = t
                                    window = [(a, b) for a, b in window if t - a < 5]
                                    span = max(t - window[0][0], 0.5) if window else 1
                                    speed = sum(b for _, b in window) / span
                                    self._speed[did] = speed
                                    cur = done + file_done
                                    self.db.execute("UPDATE downloads SET done_bytes = ?, updated_at = ? WHERE id = ?",
                                                    [cur, now(), did])
                                    self.publish("download.progress", {"id": did, "model_id": row["model_id"],
                                                                       "done_bytes": cur, "total_bytes": row["total_bytes"],
                                                                       "speed_bps": speed, "file": f["path"]})
                    if f["size"] and part.stat().st_size != f["size"]:
                        raise DownloadError(f"size mismatch for {f['path']} (got {part.stat().st_size}, expected {f['size']})")
                    part.replace(target)
                    done += target.stat().st_size
            self.db.execute("UPDATE downloads SET status = 'completed', done_bytes = ?, updated_at = ? WHERE id = ?",
                            [done, now(), did])
            self._speed.pop(did, None)
            final = self.get(did)
            self.publish("download.updated", final)
            try:
                self.on_complete(final)
            except Exception:
                log.exception("post-download hook failed")
        except _Paused:
            self.db.execute("UPDATE downloads SET done_bytes = ?, updated_at = ? WHERE id = ?", [done, now(), did])
        except Exception as exc:  # noqa: BLE001
            log.warning("download %s failed: %s", did, exc)
            self.db.execute("UPDATE downloads SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                            [str(exc), now(), did])
            self.publish("download.updated", self.get(did))
        finally:
            self._speed.pop(did, None)
            self._threads.pop(did, None)
            self._stop.pop(did, None)
            self._pump()


class _Paused(Exception):
    pass
