"""Conversations with branching message trees, streamed through llama-server or
any OpenAI-compatible endpoint, with persistent partial output."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, AsyncIterator

from ..db import Database, new_id, now
from ..events import EventBus
from ..runtime.llama import LlamaServer, stream_openai
from ..settings import Settings
from ..storage import Storage

log = logging.getLogger("sarness.chat")

SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty",
                 "frequency_penalty", "max_tokens", "seed")
TEXT_MIME = ("text/plain", "text/markdown", "application/json", "text/csv")
MAX_TEXT_ATTACHMENT = 200_000


class ChatError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ChatService:
    def __init__(self, db: Database, bus: EventBus, settings: Settings, storage: Storage, llama: LlamaServer,
                 registry, jobs):
        self.db = db
        self.bus = bus
        self.settings = settings
        self.storage = storage
        self.llama = llama
        self.registry = registry
        self.jobs = jobs
        self._stops: dict[str, asyncio.Event] = {}
        self._load_lock = asyncio.Lock()

    # ------------------------------------------------------- conversations
    def list(self, q: str | None = None, project_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
               "FROM conversations c")
        conds, args = [], []
        if q:
            conds.append("(c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.content LIKE ?))")
            args += [f"%{q}%", f"%{q}%"]
        if project_id:
            conds.append("c.project_id = ?")
            args.append(project_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?"
        args.append(limit)
        return self.db.all(sql, args)

    def create(self, title: str | None = None, model_ref: str | None = None, system_prompt: str | None = None,
               project_id: str | None = None, folder_id: str | None = None, params: dict | None = None) -> dict[str, Any]:
        row = {"id": new_id("c_"), "title": title or "New conversation", "project_id": project_id,
               "folder_id": folder_id, "pinned": 0, "model_ref": model_ref, "system_prompt": system_prompt or "",
               "params": params or {}, "active_leaf_id": None, "created_at": now(), "updated_at": now()}
        self.db.insert("conversations", row)
        return row

    def get(self, cid: str) -> dict[str, Any]:
        conv = self.db.one("SELECT * FROM conversations WHERE id = ?", [cid])
        if not conv:
            raise ChatError("conversation not found", 404)
        conv["messages"] = [self._msg_public(m) for m in
                            self.db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at", [cid])]
        return conv

    def update(self, cid: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {k: v for k, v in patch.items() if k in ("title", "pinned", "model_ref", "system_prompt", "params",
                                                            "project_id", "folder_id", "active_leaf_id")}
        if "active_leaf_id" in allowed and allowed["active_leaf_id"]:
            m = self.db.one("SELECT id FROM messages WHERE id = ? AND conversation_id = ?", [allowed["active_leaf_id"], cid])
            if not m:
                raise ChatError("message not in conversation")
        allowed["updated_at"] = now()
        self.db.update("conversations", cid, allowed)
        return self.get(cid)

    def delete(self, cid: str) -> None:
        self.db.execute("DELETE FROM conversations WHERE id = ?", [cid])

    def _msg_public(self, m: dict[str, Any]) -> dict[str, Any]:
        atts = []
        for a in m.get("attachments") or []:
            f = self.storage.get(a)
            if f:
                atts.append({"id": f["id"], "name": f.get("original_name"), "mime": f["mime"], "size": f["size"],
                             "url": self.storage.file_url(f["id"]), "width": f.get("width"), "height": f.get("height")})
        return {**m, "attachments": atts}

    # --------------------------------------------------------------- tree
    def _path_to(self, cid: str, mid: str | None) -> list[dict[str, Any]]:
        if not mid:
            return []
        rows = {m["id"]: m for m in self.db.all("SELECT * FROM messages WHERE conversation_id = ?", [cid])}
        path = []
        cur = rows.get(mid)
        while cur:
            path.append(cur)
            cur = rows.get(cur["parent_id"]) if cur["parent_id"] else None
        return list(reversed(path))

    # ------------------------------------------------------------- models
    def available_models(self) -> list[dict[str, Any]]:
        out = []
        for m in self.registry.list():
            if m["category"] == "llm":
                out.append({"ref": f"local:{m['id']}", "name": m["name"], "provider": "Sarness Engine",
                            "vision": bool((m.get("capabilities") or {}).get("vision")),
                            "context_max": (m.get("capabilities") or {}).get("context_max"),
                            "default_ctx": (m.get("capabilities") or {}).get("default_ctx"),
                            "vram_mb": m.get("vram_mb"), "size": m.get("size"), "local": True})
        for ep in self.settings.get("llm_endpoints") or []:
            for name in ep.get("models") or []:
                out.append({"ref": f"endpoint:{ep['id']}:{name}", "name": name, "provider": ep.get("name") or ep["base_url"],
                            "vision": bool(ep.get("vision")), "context_max": None, "local": False})
        return out

    async def refresh_endpoint_models(self, endpoint_id: str) -> list[str]:
        import httpx

        eps = self.settings.get("llm_endpoints") or []
        ep = next((e for e in eps if e.get("id") == endpoint_id), None)
        if not ep:
            raise ChatError("endpoint not found", 404)
        headers = {"Authorization": f"Bearer {ep['api_key']}"} if ep.get("api_key") else {}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(ep["base_url"].rstrip("/") + "/models", headers=headers)
            r.raise_for_status()
            names = sorted(m["id"] for m in r.json().get("data", []))
        ep["models"] = names
        self.settings.set("llm_endpoints", eps)
        return names

    async def ensure_local(self, model_id: str, ctx: int | None = None, gpu_layers: int | None = None) -> dict[str, Any]:
        info = self.registry.get(model_id)
        if not info or info["category"] != "llm":
            raise ChatError(f"LLM '{model_id}' is not installed", 404)
        caps = info.get("capabilities") or {}
        ctx = int(ctx or caps.get("default_ctx") or self.settings.get("llm_default_ctx"))
        ctx = min(ctx, int(caps.get("context_max") or ctx))
        ngl = int(gpu_layers if gpu_layers is not None else self.settings.get("llm_gpu_layers"))
        async with self._load_lock:
            st = self.llama.status()
            if st["state"] == "ready" and st["model_id"] == model_id and st["ctx"] == ctx and st["gpu_layers"] == ngl:
                return st
            from ..hardware import vram_free_mb

            need = int(info.get("vram_mb") or 4000) + int(self.settings.get("vram_headroom_mb"))
            if st["state"] != "stopped":
                await asyncio.to_thread(self.llama.unload)  # free the previous model first
            free = vram_free_mb() or 0
            if ngl > 0 and free < need:
                if self.jobs.supervisor.busy:
                    raise ChatError(f"The GPU is busy with a generation job ({free} MiB free, ~{need} MiB needed). "
                                    "Wait for it to finish, or set GPU layers to 0 to run on the CPU.", 409)
                if self.jobs.supervisor.alive:
                    # Stop the idle diffusion worker: this returns all of its VRAM
                    # (including the CUDA context) before llama.cpp allocates.
                    await asyncio.to_thread(self.jobs.supervisor.stop)
                    self.bus.publish("worker.status", {"loaded": [], "stopped": True})
                exps = self.jobs.experiments
                if exps is not None and exps.gpu_active():
                    await exps.yield_gpu("loading a chat model")
            self.bus.publish("llm.status", {**st, "state": "loading", "model_id": model_id})
            try:
                await asyncio.to_thread(self.llama.load, info, ctx=ctx, gpu_layers=ngl,
                                        flash_attn=bool(self.settings.get("llm_flash_attn")),
                                        fit_target_mb=max(512, int(self.settings.get("vram_headroom_mb")) + 400))
            except Exception as exc:
                self.bus.publish("llm.status", self.llama.status())
                raise ChatError(f"Could not load {info['name']}: {exc}", 500) from exc
            st = self.llama.status()
            self.bus.publish("llm.status", st)
            return st

    # ----------------------------------------------------------- generate
    def stop(self, cid: str) -> bool:
        ev = self._stops.get(cid)
        if ev:
            ev.set()
            return True
        return False

    async def generate(self, cid: str, req: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        conv = self.db.one("SELECT * FROM conversations WHERE id = ?", [cid])
        if not conv:
            raise ChatError("conversation not found", 404)
        if cid in self._stops:
            raise ChatError("a response is already being generated in this conversation", 409)
        mode = req.get("mode", "send")
        model_ref = req.get("model_ref") or conv.get("model_ref")
        if not model_ref:
            raise ChatError("choose a model first")
        params = {**(conv.get("params") or {}), **(req.get("params") or {})}

        # Resolve provider and capabilities.
        vision = False
        if model_ref.startswith("local:"):
            model_id = model_ref.split(":", 1)[1]
            info = self.registry.get(model_id)
            if not info:
                raise ChatError(f"model {model_id} is not installed", 404)
            vision = bool((info.get("capabilities") or {}).get("vision"))
        elif model_ref.startswith("endpoint:"):
            _, ep_id, ep_model = model_ref.split(":", 2)
            ep = next((e for e in (self.settings.get("llm_endpoints") or []) if e.get("id") == ep_id), None)
            if not ep:
                raise ChatError("the selected remote endpoint no longer exists", 404)
            vision = bool(ep.get("vision"))
        else:
            raise ChatError("invalid model reference")

        # Create / locate the user message.
        attachments = req.get("attachments") or []
        for a in attachments:
            f = self.storage.get(a)
            if not f:
                raise ChatError("attachment not found")
            if f["mime"].startswith("image/") and not vision:
                raise ChatError("this model does not accept images — choose a vision model (e.g. Gemma 4 E4B)")
        if mode in ("send", "edit"):
            parent = req.get("parent_id")
            if mode == "edit":
                orig = self.db.one("SELECT * FROM messages WHERE id = ? AND conversation_id = ?", [req.get("edit_of"), cid])
                if not orig or orig["role"] != "user":
                    raise ChatError("can only edit user messages")
                parent = orig["parent_id"]
            content = (req.get("content") or "").strip()
            if not content and not attachments:
                raise ChatError("message is empty")
            user_msg = {"id": new_id("m_"), "conversation_id": cid, "parent_id": parent, "role": "user",
                        "content": content, "attachments": attachments, "model_ref": None, "stats": None,
                        "status": "complete", "created_at": now()}
            self.db.insert("messages", user_msg)
            parent_id = user_msg["id"]
            yield {"type": "user_message", "message": self._msg_public(user_msg)}
            if conv["title"] in ("New conversation", ""):
                title = _title_from(content or "Attachment")
                self.db.update("conversations", cid, {"title": title})
                yield {"type": "title", "title": title}
        elif mode == "regenerate":
            parent_id = req.get("parent_id")
            pm = self.db.one("SELECT * FROM messages WHERE id = ? AND conversation_id = ?", [parent_id, cid])
            if not pm or pm["role"] != "user":
                raise ChatError("regenerate needs the user message to answer")
        else:
            raise ChatError(f"unknown mode {mode}")

        path = self._path_to(cid, parent_id)
        messages = self._build_messages(conv.get("system_prompt") or "", path, vision)
        assistant = {"id": new_id("m_"), "conversation_id": cid, "parent_id": parent_id, "role": "assistant",
                     "content": "", "reasoning": "", "attachments": [], "model_ref": model_ref, "stats": {},
                     "status": "partial", "created_at": now()}
        self.db.insert("messages", assistant)
        self.db.update("conversations", cid, {"active_leaf_id": assistant["id"], "updated_at": now(),
                                              "model_ref": model_ref})
        yield {"type": "assistant_start", "message": self._msg_public(assistant)}

        stop = asyncio.Event()
        self._stops[cid] = stop
        body: dict[str, Any] = {"messages": messages}
        for k in SAMPLING_KEYS:
            if params.get(k) is not None:
                body[k] = params[k]
        if params.get("thinking") is not None and model_ref.startswith("local:"):
            # Honoured by chat templates that support it (Qwen3, Gemma 4, …); ignored otherwise.
            body["chat_template_kwargs"] = {"enable_thinking": bool(params["thinking"])}
        elif params.get("thinking") is False:
            # OpenAI-style switch; Ollama and most reasoning-capable servers honour "none".
            body["reasoning_effort"] = "none"
        content, reasoning = [], []
        stats: dict[str, Any] = {}
        status = "complete"
        t0 = time.perf_counter()
        first_token_at = None
        last_save = time.monotonic()
        try:
            if model_ref.startswith("local:"):
                if self.llama.status()["model_id"] != model_id:
                    yield {"type": "status", "text": "Loading model…"}
                await self.ensure_local(model_id, params.get("ctx"), params.get("gpu_layers"))
                self.llama.active_streams += 1
                stream = self.llama.stream_chat({**body, "model": model_id, "timings_per_token": False})
            else:
                body["model"] = ep_model
                stream = stream_openai(ep["base_url"], ep.get("api_key"), body)
            try:
                async for chunk in stream:
                    if stop.is_set():
                        status = "stopped"
                        break
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        # llama.cpp / vLLM use reasoning_content; Ollama uses reasoning.
                        think = delta.get("reasoning_content") or delta.get("reasoning")
                        if piece or think:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                            if piece:
                                content.append(piece)
                            if think:
                                reasoning.append(think)
                            yield {"type": "delta", "content": piece or "", "reasoning": think or ""}
                        if choice.get("finish_reason"):
                            stats["finish_reason"] = choice["finish_reason"]
                    if chunk.get("timings"):
                        stats["timings"] = chunk["timings"]
                    if chunk.get("usage"):
                        stats["usage"] = chunk["usage"]
                    if time.monotonic() - last_save > 1.0:
                        last_save = time.monotonic()
                        self.db.update("messages", assistant["id"], {"content": "".join(content),
                                                                     "reasoning": "".join(reasoning)})
            finally:
                if model_ref.startswith("local:"):
                    self.llama.active_streams = max(0, self.llama.active_streams - 1)
                    self.llama.last_used = time.time()
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except ChatError as exc:
            status = "error"
            stats["error"] = str(exc)
            yield {"type": "error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("chat generation failed")
            status = "error"
            stats["error"] = str(exc)
            yield {"type": "error", "message": str(exc)}
        finally:
            self._stops.pop(cid, None)
            elapsed = time.perf_counter() - t0
            stats["elapsed_s"] = round(elapsed, 3)
            if first_token_at:
                stats["ttft_s"] = round(first_token_at - t0, 3)
            tim = stats.get("timings") or {}
            if tim.get("predicted_per_second"):
                stats["tokens_per_s"] = round(tim["predicted_per_second"], 2)
                stats["completion_tokens"] = tim.get("predicted_n")
                stats["prompt_tokens"] = tim.get("prompt_n")
            elif stats.get("usage"):
                u = stats["usage"]
                stats["completion_tokens"] = u.get("completion_tokens")
                stats["prompt_tokens"] = u.get("prompt_tokens")
                gen_time = elapsed - (stats.get("ttft_s") or 0)
                if u.get("completion_tokens") and gen_time > 0:
                    stats["tokens_per_s"] = round(u["completion_tokens"] / gen_time, 2)
            self.db.update("messages", assistant["id"], {"content": "".join(content), "reasoning": "".join(reasoning),
                                                         "status": status, "stats": stats})
            self.db.update("conversations", cid, {"updated_at": now()})
        final = self.db.one("SELECT * FROM messages WHERE id = ?", [assistant["id"]])
        yield {"type": "done", "message": self._msg_public(final)}

    # ------------------------------------------------------------- agent
    AGENT_ROLES = ("system", "user", "assistant", "tool")
    AGENT_MAX_TOOLS = 64

    def agent_models(self) -> list[dict[str, Any]]:
        """Chat models annotated with whether tool calling is expected to work."""
        out = []
        for m in self.available_models():
            out.append({**m, "tools": True if not m["local"] else None})
        return out

    def _agent_validate(self, req: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        msgs = req.get("messages")
        if not isinstance(msgs, list) or not msgs:
            raise ChatError("messages must be a non-empty list")
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") not in self.AGENT_ROLES:
                raise ChatError("every message needs a valid role")
            if m["role"] == "tool" and not m.get("tool_call_id"):
                raise ChatError("tool messages need a tool_call_id")
        tools = req.get("tools") or []
        if not isinstance(tools, list) or len(tools) > self.AGENT_MAX_TOOLS:
            raise ChatError(f"tools must be a list of at most {self.AGENT_MAX_TOOLS} definitions")
        for t in tools:
            fn = (t or {}).get("function") if isinstance(t, dict) else None
            if not isinstance(fn, dict) or t.get("type") != "function" or not fn.get("name") \
                    or not isinstance(fn.get("parameters", {}), dict):
                raise ChatError("each tool must be {type:'function', function:{name, parameters}}")
        return msgs, tools

    async def agent_completion(self, req: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stateless, tool-capable chat completion. The caller owns the transcript and
        executes every tool itself; nothing is stored and no tool runs on this host."""
        msgs, tools = self._agent_validate(req)
        model_ref = req.get("model_ref")
        if not model_ref:
            raise ChatError("choose a model first")
        params = req.get("params") or {}
        if model_ref.startswith("local:"):
            model_id = model_ref.split(":", 1)[1]
            if not self.registry.get(model_id):
                raise ChatError(f"model {model_id} is not installed", 404)
        elif model_ref.startswith("endpoint:"):
            _, ep_id, ep_model = model_ref.split(":", 2)
            ep = next((e for e in (self.settings.get("llm_endpoints") or []) if e.get("id") == ep_id), None)
            if not ep:
                raise ChatError("the selected remote endpoint no longer exists", 404)
        else:
            raise ChatError("invalid model reference")
        body: dict[str, Any] = {"messages": msgs}
        for k in SAMPLING_KEYS:
            if params.get(k) is not None:
                body[k] = params[k]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = req.get("tool_choice") or "auto"
        if params.get("thinking") is not None and model_ref.startswith("local:"):
            body["chat_template_kwargs"] = {"enable_thinking": bool(params["thinking"])}
        local = model_ref.startswith("local:")
        if local:
            await self.ensure_local(model_id, params.get("ctx"), params.get("gpu_layers"))
            self.llama.active_streams += 1
            stream = self.llama.stream_chat({**body, "model": model_id})
        else:
            stream = stream_openai(ep["base_url"], ep.get("api_key"), {**body, "model": ep_model})
        content, reasoning = [], []
        calls: dict[int, dict[str, Any]] = {}
        finish, usage, timings = None, None, None
        try:
            async for chunk in stream:
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    think = delta.get("reasoning_content") or delta.get("reasoning")
                    if piece:
                        content.append(piece)
                    if think:
                        reasoning.append(think)
                    if piece or think:
                        yield {"type": "delta", "content": piece or "", "reasoning": think or ""}
                    for tc in delta.get("tool_calls") or []:
                        i = int(tc.get("index", 0))
                        cur = calls.setdefault(i, {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            cur["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            cur["name"] = fn["name"]
                        if fn.get("arguments"):
                            cur["arguments"] += fn["arguments"]
                        yield {"type": "tool_call", "index": i, "name": cur["name"]}
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if chunk.get("timings"):
                    timings = chunk["timings"]
        finally:
            if local:
                self.llama.active_streams = max(0, self.llama.active_streams - 1)
                self.llama.last_used = time.time()
        tool_calls = [{"id": c["id"] or f"call_{new_id('')}", "type": "function",
                       "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                      for _, c in sorted(calls.items())]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        yield {"type": "done", "message": message, "reasoning": "".join(reasoning),
               "finish_reason": finish, "usage": usage, "timings": timings}

    def _build_messages(self, system: str, path: list[dict[str, Any]], vision: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system.strip():
            out.append({"role": "system", "content": system})
        for m in path:
            if m["role"] == "assistant" and m["status"] == "error" and not m["content"]:
                continue
            atts = [self.storage.get(a) for a in (m.get("attachments") or [])]
            atts = [a for a in atts if a]
            if not atts:
                out.append({"role": m["role"], "content": m["content"]})
                continue
            parts: list[dict[str, Any]] = []
            text_blocks = []
            for a in atts:
                p = self.storage.resolve(a["path"])
                if a["mime"].startswith("image/") and vision:
                    data = base64.b64encode(p.read_bytes()).decode()
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{a['mime']};base64,{data}"}})
                elif a["mime"] in TEXT_MIME:
                    txt = p.read_text(errors="replace")[:MAX_TEXT_ATTACHMENT]
                    text_blocks.append(f"<file name=\"{a.get('original_name') or 'attachment'}\">\n{txt}\n</file>")
            text = "\n\n".join([*text_blocks, m["content"]]).strip()
            if parts:
                parts.append({"type": "text", "text": text})
                out.append({"role": m["role"], "content": parts})
            else:
                out.append({"role": m["role"], "content": text})
        return out

    # ------------------------------------------------------- import/export
    def export(self, cid: str) -> dict[str, Any]:
        conv = self.get(cid)
        msgs = conv.pop("messages")
        return {"format": "sarness.conversation", "version": 1, "exported_at": now(), "conversation": conv,
                "messages": [{k: v for k, v in m.items() if k != "attachments"} | {"attachments": []} for m in msgs]}

    def export_markdown(self, cid: str) -> str:
        conv = self.get(cid)
        path = self._path_to(cid, conv.get("active_leaf_id"))
        lines = [f"# {conv['title']}", ""]
        if conv.get("system_prompt"):
            lines += ["> **System:** " + conv["system_prompt"].replace("\n", "\n> "), ""]
        for m in path:
            who = "You" if m["role"] == "user" else "Assistant"
            lines += [f"## {who}", "", m["content"], ""]
        return "\n".join(lines)

    def import_(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("format") != "sarness.conversation":
            raise ChatError("not a Sarness conversation export")
        src = data["conversation"]
        conv = self.create(title=src.get("title"), model_ref=src.get("model_ref"), system_prompt=src.get("system_prompt"),
                           params=src.get("params"))
        idmap: dict[str, str] = {}
        for m in sorted(data.get("messages", []), key=lambda m: m.get("created_at", 0)):
            nid = new_id("m_")
            idmap[m["id"]] = nid
            self.db.insert("messages", {
                "id": nid, "conversation_id": conv["id"], "parent_id": idmap.get(m.get("parent_id")),
                "role": m["role"], "content": m.get("content", ""), "reasoning": m.get("reasoning"),
                "attachments": [], "model_ref": m.get("model_ref"), "stats": m.get("stats"),
                "status": m.get("status", "complete"), "created_at": m.get("created_at", now())})
        leaf = idmap.get(src.get("active_leaf_id"))
        self.db.update("conversations", conv["id"], {"active_leaf_id": leaf})
        return self.get(conv["id"])


def _title_from(text: str) -> str:
    t = " ".join(text.split())
    return (t[:60] + "…") if len(t) > 60 else t
