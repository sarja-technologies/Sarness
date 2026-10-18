"""SNP operation catalogue and in-process dispatch (docs/SNP.md section 7).

``ops.json`` maps every operation to the engine's route function (``"api.chat:list_conversations"``).
Unary operations call that function directly: path/query parameters come from ``args``, the
request body from ``args["body"]`` and ``st`` is the shared :class:`AppState`. Nothing goes
through HTTP and no ASGI request is built. Streaming, binary and subscription operations are
implemented natively here because they need SNP flow control instead of an HTTP response.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import json
import logging
import math
import os
import secrets
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from starlette.requests import Request
from starlette.responses import Response

from .. import API_VERSION
from .. import storage as storage_mod

log = logging.getLogger("sarness.snp")

CATALOGUE_PATH = Path(__file__).with_name("ops.json")
TELEMETRY = frozenset({"system.stats", "job.progress", "job.preview", "download.progress"})
# Operations that legitimately run longer than the default unary deadline (spec 6.5).
LONG_RUNNING_PREFIXES = ("llm.", "paintbrush.", "runtime.", "model.", "worker.", "hf.", "gateway.", "experiment.")

CODE_STATUS = {"bad_request": 400, "unauthenticated": 401, "not_found": 404, "conflict": 409, "unknown_op": 404,
               "too_large": 413, "rate_limited": 429, "resource_exhausted": 503, "cancelled": 499,
               "deadline_exceeded": 504, "stalled": 504, "unavailable": 503, "internal": 500}
RETRYABLE = frozenset({"rate_limited", "resource_exhausted", "deadline_exceeded", "stalled", "unavailable"})


class OpError(Exception):
    """An operation failure that maps to an SNP ERROR frame (spec 6.3)."""

    def __init__(self, code: str, message: str, status: int | None = None, retryable: bool | None = None,
                 detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message[:1024]
        self.status = status if status is not None else CODE_STATUS.get(code, 500)
        self.retryable = code in RETRYABLE if retryable is None else retryable
        self.detail = detail

    def wire(self) -> dict[str, Any]:
        out = {"code": self.code, "status": self.status, "message": self.message, "retryable": self.retryable}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


def code_for_status(status: int) -> str:
    if status in (400, 405, 415, 416, 422, 403):
        return "bad_request"
    if status == 401:
        return "unauthenticated"
    if status in (404, 410):
        return "not_found"
    if status in (408, 504):
        return "deadline_exceeded"
    if status == 409:
        return "conflict"
    if status == 413:
        return "too_large"
    if status == 429:
        return "rate_limited"
    if status == 499:
        return "cancelled"
    if status == 503:
        return "resource_exhausted"
    if status in (502,):
        return "unavailable"
    if 400 <= status < 500:
        return "bad_request"
    return "internal"


def from_status(status: int, message: str, detail: Any = None) -> OpError:
    return OpError(code_for_status(status), message, status=status, detail=detail)


def map_exception(exc: BaseException) -> OpError:
    """Translates what a route function raises into an SNP error (never leaks internals for faults)."""
    from ..chat.service import ChatError
    from ..jobs.service import JobError
    from ..models.downloads import DownloadError
    from ..storage import StorageError

    if isinstance(exc, OpError):
        return exc
    if isinstance(exc, HTTPException):
        d = exc.detail
        return from_status(exc.status_code, d if isinstance(d, str) else json.dumps(d, default=str))
    if isinstance(exc, (ChatError, JobError)):
        issues = getattr(exc, "issues", None)
        return from_status(exc.status, str(exc), {"issues": issues} if issues else None)
    if isinstance(exc, StorageError):
        return from_status(413 if "too large" in str(exc) else 400, str(exc))
    if isinstance(exc, DownloadError):
        return from_status(400, str(exc))
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status < 600 and type(exc).__name__.endswith("Error"):
        return from_status(status, str(exc))
    if isinstance(exc, ValidationError):
        return OpError("bad_request", "invalid arguments", status=422)
    log.exception("operation failed")
    return OpError("internal", "internal error")


def plain(obj: Any) -> Any:
    """Makes a handler result encodable by SNP-CBOR (same data model as the JSON API)."""
    if obj is None or isinstance(obj, (bool, str, bytes)):
        return obj
    if isinstance(obj, int):
        return obj if -(2**63) <= obj < 2**63 else float(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k if isinstance(k, str) else str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [plain(v) for v in obj]
    if isinstance(obj, Response):
        body = bytes(obj.body)
        if (obj.media_type or "").endswith("json"):
            return plain(json.loads(body))
        return body.decode("utf-8", "replace")
    return plain(jsonable_encoder(obj))


# --------------------------------------------------------------------------- catalogue
@dataclass
class Param:
    name: str
    kind: str  # "state" | "request" | "arg"
    required: bool = False
    default: Any = None
    adapter: TypeAdapter | None = None


@dataclass
class OpDef:
    name: str
    method: str
    route: str
    handler: str
    mode: str
    params: list[str]
    fn: Callable | None = None
    native: Callable[..., Awaitable[Any]] | None = None
    spec: list[Param] = field(default_factory=list)
    error: str | None = None

    @property
    def long_running(self) -> bool:
        return self.name.startswith(LONG_RUNNING_PREFIXES)


def resolve_handler(path: str) -> Callable:
    module, _, name = path.partition(":")
    mod = importlib.import_module(f"sarness_engine.{module}")
    return getattr(mod, name)


def _param_specs(fn: Callable) -> list[Param]:
    from ..api.deps import AppState

    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolved forward references: validate nothing
        hints = {}
    out: list[Param] = []
    for p in inspect.signature(fn).parameters.values():
        hint = hints.get(p.name, Any)
        if p.name == "st" or hint is AppState:
            out.append(Param(p.name, "state"))
            continue
        if hint is Request:
            out.append(Param(p.name, "request"))
            continue
        default, meta = p.default, []
        required = default is inspect.Parameter.empty
        if isinstance(default, FieldInfo):  # Query(...), Body(...), Path(...)
            meta = list(default.metadata)
            required = default.is_required()
            default = None if required else default.get_default(call_default_factory=True)
        try:
            adapter = TypeAdapter(typing.Annotated[(hint, *meta)] if meta else hint)
        except Exception:  # noqa: BLE001
            adapter = None
        out.append(Param(p.name, "arg", required, default, adapter))
    return out


class Catalogue:
    def __init__(self, path: Path | None = None):
        raw = json.loads((path or CATALOGUE_PATH).read_text())
        if raw.get("protocol") != "SNP/1":
            raise ValueError("unsupported catalogue schema")
        self.ops: dict[str, OpDef] = {}
        for o in raw["ops"]:
            op = OpDef(o["op"], o["method"], o["route"], o["handler"], o["mode"], list(o.get("params", [])))
            if op.name in self.ops:
                raise ValueError(f"duplicate operation {op.name}")
            try:
                op.fn = resolve_handler(op.handler)
                if op.name in NATIVE:
                    op.native = NATIVE[op.name]
                elif op.mode == "unary":
                    op.spec = _param_specs(op.fn)
                else:
                    op.error = "no native implementation"
            except Exception as exc:  # noqa: BLE001 - one broken entry must not take the server down
                op.error = f"cannot resolve handler: {exc}"
            if op.error:
                log.error("SNP operation %s unavailable: %s", op.name, op.error)
            self.ops[op.name] = op

    def get(self, name: str) -> OpDef | None:
        return self.ops.get(name)


async def call_unary(op: OpDef, args: dict[str, Any], st: Any) -> Any:
    """Runs the route function in-process; sync functions go to a worker thread like in FastAPI."""
    if op.error or op.fn is None:
        raise OpError("internal", f"operation {op.name} is not available on this server")
    kwargs: dict[str, Any] = {}
    for p in op.spec:
        if p.kind == "state":
            kwargs[p.name] = st
        elif p.kind == "request":
            kwargs[p.name] = None  # only the unauthenticated health probe takes one, and ignores it
        elif p.name in args:
            value = args[p.name]
            if p.adapter is not None:
                try:
                    value = p.adapter.validate_python(value)
                except ValidationError as exc:
                    first = exc.errors()[0]
                    raise OpError("bad_request", f"invalid argument '{p.name}': {first.get('msg', 'invalid')}",
                                  status=422) from None
            kwargs[p.name] = value
        elif p.required:
            raise OpError("bad_request", f"missing required argument '{p.name}'")
        else:
            kwargs[p.name] = copy.deepcopy(p.default)
    try:
        if inspect.iscoroutinefunction(op.fn):
            result = await op.fn(**kwargs)
        else:
            result = await asyncio.to_thread(op.fn, **kwargs)
    except OpError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from None
    try:
        return plain(result)
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from None


# --------------------------------------------------------------------------- native helpers
def _str_arg(args: dict[str, Any], name: str) -> str:
    v = args.get(name)
    if not isinstance(v, str) or not v:
        raise OpError("bad_request", f"missing required argument '{name}'")
    return v


def _map_arg(args: dict[str, Any], name: str) -> dict[str, Any]:
    v = args.get(name)
    if not isinstance(v, dict):
        raise OpError("bad_request", f"missing required argument '{name}'")
    return v


async def _abort_gen(gen: Any) -> None:
    """Terminates a suspended async generator the way a cancelled HTTP request would: the
    generator sees ``CancelledError`` at its ``yield`` (chat then persists status ``stopped``)."""
    if inspect.getasyncgenstate(gen) != inspect.AGEN_SUSPENDED:
        return
    try:
        await gen.athrow(asyncio.CancelledError())
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:  # noqa: BLE001
        log.exception("generator cleanup failed")


async def _pump(ctx: Any, gen: Any, first_error: Callable[[Exception], OpError], mid_error: Callable[[Exception], str],
                on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
    """Moves events from an engine async generator to MSG frames.

    The next event is only requested once the previous MSG has been admitted by flow control,
    so a slow client slows the model stream itself. The first event is awaited before any MSG
    so that validation failures become ERROR frames, as the HTTP routes do."""
    try:
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            raise OpError("internal", "no response") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise first_error(exc) from None
        ev = first
        while True:
            if on_event:
                on_event(ev)
            await ctx.send_msg(plain(ev))
            try:
                ev = await gen.__anext__()
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await ctx.send_msg({"type": "error", "message": mid_error(exc)})
                return
    finally:
        await _abort_gen(gen)


async def chat_generate(ctx: Any, args: dict[str, Any]) -> Any:
    from ..chat.service import ChatError

    st = ctx.st
    gen = st.chat.generate(_str_arg(args, "cid"), _map_arg(args, "body"))
    seen: dict[str, str] = {}

    def note(ev: dict[str, Any]) -> None:
        if ev.get("type") == "assistant_start":
            seen["assistant"] = ev["message"]["id"]

    def first_error(exc: Exception) -> OpError:
        if isinstance(exc, ChatError):
            return from_status(exc.status, str(exc))
        return map_exception(exc)

    def mid_error(exc: Exception) -> str:
        if isinstance(exc, ChatError):
            return str(exc)
        log.exception("chat stream failed")
        return "generation failed"

    try:
        await _pump(ctx, gen, first_error, mid_error, note)
    finally:
        mid = seen.get("assistant")
        if mid:
            # Cancelled between assistant_start and the generator's own try block: never leave 'partial'.
            row = st.db.one("SELECT status FROM messages WHERE id = ?", [mid])
            if row and row["status"] == "partial":
                st.db.update("messages", mid, {"status": "stopped"})
    return None


async def agent_completion(ctx: Any, args: dict[str, Any]) -> Any:
    from ..chat.service import ChatError

    gen = ctx.st.chat.agent_completion(_map_arg(args, "body"))

    def first_error(exc: Exception) -> OpError:
        if isinstance(exc, ChatError):
            return from_status(exc.status, str(exc))
        return OpError("unavailable", f"model server error: {exc}", status=502)

    await _pump(ctx, gen, first_error, lambda exc: str(exc))
    return None


class Topics:
    """Subscription filter. A topic is an event type or a prefix ending in '.' (spec 7.1)."""

    def __init__(self, topics: list[str] | None):
        self.all = topics is None
        self.include: set[str] = set(topics or [])
        self.exclude: set[str] = set()

    @staticmethod
    def _hit(topic: str, etype: str) -> bool:
        return etype.startswith(topic) if topic.endswith(".") else etype == topic

    def matches(self, etype: str) -> bool:
        if self.all:
            return not any(self._hit(t, etype) for t in self.exclude)
        return any(self._hit(t, etype) for t in self.include)

    def subscribe(self, topics: list[str]) -> None:
        for t in topics:
            if self.all:
                self.exclude.discard(t)
            else:
                self.include.add(t)

    def unsubscribe(self, topics: list[str]) -> None:
        for t in topics:
            if self.all:
                self.exclude.add(t)
            else:
                self.include.discard(t)


def _topic_list(v: Any, what: str) -> list[str]:
    if not isinstance(v, list) or len(v) > 256 or not all(isinstance(t, str) and 0 < len(t) <= 128 for t in v):
        raise OpError("bad_request", f"{what} must be a list of at most 256 topic names")
    return v


async def _sub_control(ctx: Any, topics: Topics) -> None:
    while True:
        kind, value, nbytes = await ctx.inbox.get()
        if nbytes:
            ctx.consumed(nbytes)
        if kind == "end":
            return
        if kind != "msg" or not isinstance(value, dict):
            raise OpError("bad_request", "subscription control messages must be maps")
        if "subscribe" in value:
            topics.subscribe(_topic_list(value["subscribe"], "subscribe"))
        if "unsubscribe" in value:
            topics.unsubscribe(_topic_list(value["unsubscribe"], "unsubscribe"))


async def event_subscribe(ctx: Any, args: dict[str, Any]) -> Any:
    raw = args.get("topics")
    topics = Topics(None if raw is None else _topic_list(raw, "topics"))
    st = ctx.st
    q = st.bus.subscribe()
    control = asyncio.ensure_future(_sub_control(ctx, topics))
    getter: asyncio.Future | None = None
    dropped = 0
    try:
        await ctx.send_msg({"type": "hello", "data": {"api_version": API_VERSION}})
        while True:
            if getter is None:
                getter = asyncio.ensure_future(q.get())
            await asyncio.wait({getter, control}, return_when=asyncio.FIRST_COMPLETED)
            if control.done():
                control.result()  # client END (clean) or a control error
                return None
            ev = getter.result()
            getter = None
            if not topics.matches(ev["type"]):
                continue
            msg = {"type": ev["type"], "ts": ev["ts"], "data": plain(ev["data"])}
            if dropped:
                msg["dropped"] = dropped
            droppable = ev["type"] in TELEMETRY
            if droppable:
                if not ctx.try_send_msg(msg):
                    dropped += 1
                else:
                    dropped = 0
            else:
                await ctx.send_msg(msg)
                dropped = 0
    finally:
        for fut in (getter, control):
            if fut is not None and not fut.done():
                fut.cancel()
        st.bus.unsubscribe(q)


async def file_get(ctx: Any, args: dict[str, Any]) -> Any:
    st = ctx.st
    fid = _str_arg(args, "fid")
    rng = args.get("range")
    if rng is not None:
        if not isinstance(rng, dict) or not isinstance(rng.get("start"), int) or rng["start"] < 0 or \
                not (rng.get("end") is None or (isinstance(rng["end"], int) and rng["end"] >= rng["start"])):
            raise OpError("bad_request", "range must be {start, end?} with 0 <= start <= end")
    f = await asyncio.to_thread(st.storage.get, fid)
    if not f:
        raise OpError("not_found", "file not found")
    path = st.storage.resolve(f["path"])
    try:
        fh = await asyncio.to_thread(open, path, "rb")
    except FileNotFoundError:
        raise from_status(410, "file is missing on disk") from None
    try:
        size = os.fstat(fh.fileno()).st_size
        start = rng["start"] if rng else 0
        end = size - 1
        if rng and rng.get("end") is not None:
            end = min(rng["end"], size - 1)
        if size and start >= size:
            raise from_status(416, "range not satisfiable")
        length = max(0, end - start + 1) if size else 0
        meta = {"mime": f["mime"], "size": size, "start": start, "end": max(end, 0) if size else 0, "length": length}
        name = f.get("original_name") or path.name
        if name:
            meta["name"] = name
        await ctx.send_msg({"meta": meta})
        remaining, chunk = length, ctx.chunk_size
        await asyncio.to_thread(fh.seek, start)
        while remaining > 0:
            data = await asyncio.to_thread(fh.read, min(chunk, remaining))
            if not data:
                raise OpError("internal", "file ended unexpectedly")
            remaining -= len(data)
            await ctx.send_data(data)
    finally:
        fh.close()
    return None


async def file_upload(ctx: Any, args: dict[str, Any]) -> Any:
    st = ctx.st
    name = args.get("name")
    mime = args.get("mime")
    declared = args.get("size")
    if (name is not None and not isinstance(name, str)) or (mime is not None and not isinstance(mime, str)) or \
            (declared is not None and (not isinstance(declared, int) or declared < 0)):
        raise OpError("bad_request", "name and mime must be text, size a non-negative integer")
    limit = storage_mod.MAX_UPLOAD_BYTES
    if declared is not None and declared > limit:
        raise OpError("too_large", "upload too large")
    st.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = st.cfg.tmp_dir / f"snp-upload-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    fh = os.fdopen(fd, "wb")
    received = 0
    committed = False
    try:
        while True:
            kind, value, nbytes = await ctx.inbox.get()
            if kind == "end":
                break
            if kind != "data":
                raise OpError("bad_request", "unexpected MSG on an upload")
            received += len(value)
            if received > limit:
                if nbytes:
                    ctx.consumed(nbytes)
                raise OpError("too_large", "upload too large")
            await asyncio.to_thread(fh.write, value)
            ctx.consumed(nbytes)
        fh.close()
        if declared is not None and declared != received:
            raise OpError("bad_request", f"declared size {declared} does not match the {received} octets received")
        # From here the upload is complete; finish the commit even if the stream is cancelled meanwhile.
        row = await asyncio.shield(asyncio.to_thread(st.storage.save_upload_file, tmp, name or "upload", mime))
        committed = True
        return {**row, "url": st.storage.file_url(row["id"])}
    except OpError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from None
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        if not committed:
            tmp.unlink(missing_ok=True)


NATIVE: dict[str, Callable[..., Awaitable[Any]]] = {
    "chat.generate": chat_generate,
    "agent.completion": agent_completion,
    "event.subscribe": event_subscribe,
    "file.get": file_get,
    "file.upload": file_upload,
}
