"""Saved experiment documents (e.g. a God's Paintbrush painting).

Stored in the engine database like conversations and workflows, so they live
with the rest of the user's data on the server and survive desktop reinstalls.
``data`` is experiment-defined JSON; ``thumbnail`` is a small data URL.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database, new_id, now

MAX_DATA_BYTES = 8 * 1024 * 1024
MAX_THUMB_BYTES = 512 * 1024
MAX_TITLE = 200


class DocumentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Documents:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _check(data: Any = None, thumbnail: Any = None, title: Any = None) -> None:
        if data is not None:
            if not isinstance(data, dict):
                raise DocumentError("data must be a JSON object")
            if len(json.dumps(data, separators=(",", ":"))) > MAX_DATA_BYTES:
                raise DocumentError("document is too large (8 MB limit)", 413)
        if thumbnail is not None and thumbnail != "":
            if not isinstance(thumbnail, str) or not thumbnail.startswith("data:image/"):
                raise DocumentError("thumbnail must be an image data URL")
            if len(thumbnail) > MAX_THUMB_BYTES:
                raise DocumentError("thumbnail is too large (512 KB limit)", 413)
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise DocumentError("title must be a non-empty string")

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {k: row[k] for k in ("id", "experiment_id", "title", "thumbnail", "project_id", "created_at", "updated_at")}

    def list(self, experiment_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT id, experiment_id, title, thumbnail, project_id, created_at, updated_at "
                           "FROM experiment_documents WHERE experiment_id = ? ORDER BY updated_at DESC LIMIT ?",
                           [experiment_id, limit])
        return [self._summary(r) for r in rows]

    def get(self, experiment_id: str, doc_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM experiment_documents WHERE id = ? AND experiment_id = ?", [doc_id, experiment_id])
        if not row:
            raise DocumentError("document not found", 404)
        return row

    def create(self, experiment_id: str, title: str, data: dict[str, Any], thumbnail: str | None = None,
               project_id: str | None = None) -> dict[str, Any]:
        self._check(data, thumbnail, title)
        t = now()
        row = {"id": new_id("xd_"), "experiment_id": experiment_id, "title": title.strip()[:MAX_TITLE], "data": data,
               "thumbnail": thumbnail or None, "project_id": project_id, "created_at": t, "updated_at": t}
        self.db.insert("experiment_documents", row)
        return self.get(experiment_id, row["id"])

    def update(self, experiment_id: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        self.get(experiment_id, doc_id)
        values: dict[str, Any] = {}
        if "title" in patch:
            self._check(title=patch["title"])
            values["title"] = patch["title"].strip()[:MAX_TITLE]
        if "data" in patch:
            self._check(data=patch["data"])
            values["data"] = patch["data"]
        if "thumbnail" in patch:
            self._check(thumbnail=patch["thumbnail"])
            values["thumbnail"] = patch["thumbnail"] or None
        if "project_id" in patch:
            values["project_id"] = patch["project_id"]
        values["updated_at"] = now()
        self.db.update("experiment_documents", doc_id, values)
        return self.get(experiment_id, doc_id)

    def duplicate(self, experiment_id: str, doc_id: str, title: str | None = None) -> dict[str, Any]:
        src = self.get(experiment_id, doc_id)
        return self.create(experiment_id, title or f"{src['title']} (copy)", src["data"], src.get("thumbnail"),
                           src.get("project_id"))

    def delete(self, experiment_id: str, doc_id: str) -> None:
        self.get(experiment_id, doc_id)
        self.db.execute("DELETE FROM experiment_documents WHERE id = ?", [doc_id])
