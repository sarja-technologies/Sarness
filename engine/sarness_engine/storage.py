"""Content storage: generated outputs, uploads, thumbnails and signed file URLs.

All paths stored in the database are *relative* to the engine data directory, and
every resolve goes through :meth:`Storage.resolve`, which refuses anything that
escapes the data directory.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .config import EngineConfig
from .db import Database, new_id, now

log = logging.getLogger("sarness.storage")

THUMB_SIZE = 512
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
ALLOWED_UPLOAD_MIME = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
    "video/mp4", "video/webm", "video/quicktime",
    "text/plain", "text/markdown", "application/json", "text/csv", "application/pdf",
}


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, cfg: EngineConfig, db: Database):
        self.cfg = cfg
        self.db = db

    # ---- paths --------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        root = self.cfg.root.resolve()
        p = (root / rel).resolve()
        if root != p and root not in p.parents:
            raise StorageError("path escapes data directory")
        return p

    def rel(self, p: Path) -> str:
        return str(Path(p).resolve().relative_to(self.cfg.root.resolve()))

    def output_path(self, kind: str, ext: str) -> Path:
        t = time.localtime()
        d = self.cfg.outputs_dir / ("images" if kind == "image" else "videos") / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", t)
        return d / f"sarness-{stamp}-{new_id()[:8]}.{ext}"

    # ---- registration ----------------------------------------------------
    def register(self, path: Path, kind: str, *, mime: str | None = None,
                 original_name: str | None = None, **dims: Any) -> dict[str, Any]:
        path = Path(path)
        data_hash = _sha256(path)
        mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        row = {
            "id": new_id("f_"),
            "kind": kind,
            "path": self.rel(path),
            "mime": mime,
            "size": path.stat().st_size,
            "sha256": data_hash,
            "width": dims.get("width"),
            "height": dims.get("height"),
            "duration": dims.get("duration"),
            "original_name": original_name,
            "created_at": now(),
        }
        self.db.insert("files", row)
        return row

    def get(self, file_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM files WHERE id = ?", [file_id])

    # ---- uploads ----------------------------------------------------------
    def save_upload(self, data: bytes, filename: str, declared_mime: str | None) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise StorageError("upload too large")
        mime = _sniff_mime(data) or declared_mime or ""
        if mime not in ALLOWED_UPLOAD_MIME:
            raise StorageError(f"unsupported upload type: {mime or 'unknown'}")
        dest = self._upload_dest(mime)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return self._finish_upload(dest, mime, filename)

    def save_upload_file(self, src: Path, filename: str, declared_mime: str | None) -> dict[str, Any]:
        """Like :meth:`save_upload` for data that is already on disk (streamed uploads).

        ``src`` must lie on the same filesystem as the uploads directory; it is moved, never copied,
        and removed when the upload is rejected."""
        src = Path(src)
        try:
            if src.stat().st_size > MAX_UPLOAD_BYTES:
                raise StorageError("upload too large")
            with open(src, "rb") as f:
                head = f.read(65536 + 16)
            mime = _sniff_mime(head) or declared_mime or ""
            if mime not in ALLOWED_UPLOAD_MIME:
                raise StorageError(f"unsupported upload type: {mime or 'unknown'}")
            dest = self._upload_dest(mime)
            os.chmod(src, 0o600)
            os.replace(src, dest)
        except BaseException:
            src.unlink(missing_ok=True)
            raise
        return self._finish_upload(dest, mime, filename)

    def _upload_dest(self, mime: str) -> Path:
        ext = mimetypes.guess_extension(mime) or ".bin"
        if ext == ".jpe":
            ext = ".jpg"
        self.cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.uploads_dir / f"{new_id('u_')}{ext}"

    def _finish_upload(self, dest: Path, mime: str, filename: str) -> dict[str, Any]:
        dims: dict[str, Any] = {}
        if mime.startswith("image/"):
            try:
                with Image.open(dest) as im:
                    im.verify()
                with Image.open(dest) as im:
                    dims = {"width": im.width, "height": im.height}
            except Exception as exc:
                dest.unlink(missing_ok=True)
                raise StorageError(f"invalid image: {exc}") from exc
        safe_name = Path(filename or "upload").name[:200]
        return self.register(dest, "upload", mime=mime, original_name=safe_name, **dims)

    # ---- thumbnails ----------------------------------------------------------
    def make_image_thumb(self, src: Path) -> dict[str, Any]:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            dest = self.cfg.thumbs_dir / f"{new_id('t_')}.webp"
            im.save(dest, "WEBP", quality=82, method=4)
            return self.register(dest, "thumb", mime="image/webp", width=im.width, height=im.height)

    def make_video_thumb(self, src: Path) -> dict[str, Any] | None:
        try:
            import imageio.v3 as iio

            frame = iio.imread(src, index=0, plugin="pyav") if _has_pyav() else None
        except Exception:
            frame = None
        if frame is None:
            try:
                import imageio.v2 as iio2

                reader = iio2.get_reader(str(src), format="ffmpeg")
                frame = reader.get_data(0)
                reader.close()
            except Exception as exc:
                log.warning("video thumbnail failed: %s", exc)
                return None
        im = Image.fromarray(frame)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        dest = self.cfg.thumbs_dir / f"{new_id('t_')}.webp"
        im.convert("RGB").save(dest, "WEBP", quality=82)
        return self.register(dest, "thumb", mime="image/webp", width=im.width, height=im.height)

    # ---- signed URLs ------------------------------------------------------------
    def sign(self, file_id: str, ttl: int = 3600 * 6) -> str:
        exp = int(time.time()) + ttl
        sig = hmac.new(self.cfg.signing_key.encode(), f"{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"/v1/files/{file_id}?exp={exp}&sig={sig}"

    def verify(self, file_id: str, exp: str | None, sig: str | None) -> bool:
        if not exp or not sig:
            return False
        try:
            if int(exp) < time.time():
                return False
        except ValueError:
            return False
        expected = hmac.new(self.cfg.signing_key.encode(), f"{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(expected, sig)

    def file_url(self, file_id: str | None) -> str | None:
        """Stable, token-authenticated URL (the desktop proxy injects the token)."""
        return f"/v1/files/{file_id}" if file_id else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_pyav() -> bool:
    try:
        import av  # noqa: F401

        return True
    except ImportError:
        return False


def _sniff_mime(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:2] == b"BM":
        return "image/bmp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[:5] == b"%PDF-":
        return "application/pdf"
    try:
        text = data[:65536].decode("utf-8")
        if "\x00" in text:
            return None
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            return "application/json"
        return "text/plain"
    except UnicodeDecodeError:
        return None


def image_to_bytes(im: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()
