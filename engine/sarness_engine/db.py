"""SQLite persistence layer.

A single connection in WAL mode guarded by a re-entrant lock. Every query in the
engine is small, so serialising access is simpler and safer than a pool.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT, icon TEXT,
        sort INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE folders (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
        name TEXT NOT NULL, sort INTEGER DEFAULT 0, created_at REAL NOT NULL
    );
    CREATE INDEX folders_project ON folders(project_id);

    CREATE TABLE files (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, mime TEXT NOT NULL,
        size INTEGER, sha256 TEXT, width INTEGER, height INTEGER, duration REAL,
        original_name TEXT, created_at REAL NOT NULL
    );

    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
        project_id TEXT, folder_id TEXT, title TEXT,
        workflow TEXT NOT NULL, simple TEXT,
        progress REAL DEFAULT 0, progress_text TEXT, current_node TEXT,
        node_states TEXT, error TEXT, attempts INTEGER DEFAULT 0,
        vram_estimate_mb INTEGER, retry_of TEXT, result TEXT,
        created_at REAL NOT NULL, started_at REAL, finished_at REAL
    );
    CREATE INDEX jobs_status ON jobs(status, created_at);

    CREATE TABLE generations (
        id TEXT PRIMARY KEY, job_id TEXT, type TEXT NOT NULL,
        project_id TEXT, folder_id TEXT,
        file_id TEXT NOT NULL, thumb_file_id TEXT,
        prompt TEXT, negative_prompt TEXT, model_id TEXT, seed INTEGER,
        width INTEGER, height INTEGER, frames INTEGER, fps REAL, duration REAL,
        metadata TEXT, workflow TEXT, simple TEXT,
        favorite INTEGER DEFAULT 0, deleted_at REAL, created_at REAL NOT NULL
    );
    CREATE INDEX gen_type_created ON generations(type, created_at DESC);
    CREATE INDEX gen_project ON generations(project_id);
    CREATE INDEX gen_job ON generations(job_id);

    CREATE TABLE tags (
        generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
        tag TEXT NOT NULL, PRIMARY KEY (generation_id, tag)
    );
    CREATE TABLE collections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE TABLE collection_items (
        collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
        added_at REAL NOT NULL, PRIMARY KEY (collection_id, generation_id)
    );

    CREATE TABLE workflows (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, description TEXT,
        graph TEXT NOT NULL, is_preset INTEGER DEFAULT 0, project_id TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    );

    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT, folder_id TEXT,
        pinned INTEGER DEFAULT 0, model_ref TEXT, system_prompt TEXT, params TEXT,
        active_leaf_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        parent_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', reasoning TEXT,
        attachments TEXT, model_ref TEXT, stats TEXT, status TEXT NOT NULL DEFAULT 'complete',
        created_at REAL NOT NULL
    );
    CREATE INDEX messages_conv ON messages(conversation_id, created_at);

    CREATE TABLE models (
        id TEXT PRIMARY KEY, catalog_id TEXT, name TEXT NOT NULL, category TEXT NOT NULL,
        family TEXT, format TEXT NOT NULL, path TEXT NOT NULL, size INTEGER,
        source TEXT, meta TEXT, installed_at REAL NOT NULL
    );

    CREATE TABLE downloads (
        id TEXT PRIMARY KEY, model_id TEXT NOT NULL, catalog_id TEXT, name TEXT NOT NULL,
        category TEXT NOT NULL, repo TEXT NOT NULL, revision TEXT, files TEXT NOT NULL,
        dest TEXT NOT NULL, total_bytes INTEGER, done_bytes INTEGER DEFAULT 0,
        status TEXT NOT NULL, error TEXT, meta TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    """,
    # 2 — saved experiment documents (e.g. God's Paintbrush paintings)
    """
    CREATE TABLE experiment_documents (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, title TEXT NOT NULL,
        data TEXT NOT NULL, thumbnail TEXT, project_id TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE INDEX experiment_documents_exp ON experiment_documents(experiment_id, updated_at DESC);
    """,
]

JSON_COLUMNS = {
    "workflow", "simple", "node_states", "error", "result", "metadata", "attachments",
    "stats", "params", "source", "meta", "files", "graph", "data",
}


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for idx, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript("BEGIN;" + script + f"; PRAGMA user_version={idx}; COMMIT;")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for k, v in out.items():
            if k in JSON_COLUMNS and isinstance(v, str):
                try:
                    out[k] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        return out

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        if isinstance(value, bool):
            return int(value)
        return value

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, [self._encode(p) for p in params])

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._decode(self._conn.execute(sql, [self._encode(p) for p in params]).fetchone())

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, [self._encode(p) for p in params]).fetchall()
            return [self._decode(r) for r in rows]  # type: ignore[misc]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, [self._encode(p) for p in params]).fetchone()
            return row[0] if row else None

    def insert(self, table: str, values: dict[str, Any]) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    def update(self, table: str, key: str, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), key])

    def transaction(self):
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
