"""Model selection and eviction-safe preparation, shared by the CLI and the REPL."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .agent import RunConfig
from .modelinfo import capabilities


class ModelError(RuntimeError):
    pass


def resolve_model(models: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    """Exact ref, exact name, then a unique case-insensitive substring of ref or name."""
    if not models:
        raise ModelError("The engine reports no chat models")
    sel = selector.strip()
    exact = [m for m in models if m.get("ref") == sel] or [m for m in models if str(m.get("name", "")).lower() == sel.lower()]
    if not exact and not sel.startswith(("local:", "endpoint:")):
        exact = [m for m in models if m.get("ref") == f"local:{sel}"]
    if len(exact) == 1:
        return exact[0]
    hits = exact or [m for m in models if sel.lower() in m.get("ref", "").lower() or sel.lower() in str(m.get("name", "")).lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ModelError(f"No model matches '{selector}'. Run 'sarness models' to see what is installed.")
    raise ModelError(f"'{selector}' matches {len(hits)} models; be more specific:\n  " + "\n  ".join(m["ref"] for m in hits[:8]))


@dataclass
class Prepared:
    ctx: int
    gpu_layers: int | None
    pin: bool            # send ctx/gpu_layers with every request so the engine never reloads
    assumed: bool        # context size is a guess (remote endpoint model)
    notes: list[str]
    will_load: bool
    replaces: str | None = None   # id of the loaded model the user agreed to replace (so the per-turn guard stays quiet about it)


def prepare_model(engine: Any, model: dict[str, Any], *, wanted_ctx: int | None, confirm_swap: Callable[[str], bool] | None,
                  allow_swap: bool = False) -> Prepared:
    """Decide the context window and make sure using this model cannot evict someone else's.

    The engine reloads whenever the requested ctx/gpu_layers differ from the loaded ones - even for the
    same model - so for an already-loaded model we pin to its exact settings. Loading over a *different*
    loaded model unloads it; that needs an explicit yes, and is refused while it is generating."""
    notes: list[str] = []
    if not model.get("local"):
        return Prepared(int(wanted_ctx or model.get("context_max") or 32768), None, False, not model.get("context_max"),
                        ["remote endpoint model: context size is not enforced by this engine"], False)
    st = engine.llm_status()
    loaded = st.get("model_id")
    ref_id = model["ref"].split(":", 1)[1]
    state = st.get("state")
    default_ctx = int(model.get("default_ctx") or 8192)
    cmax = int(model.get("context_max") or 0) or None
    if state == "ready" and loaded == ref_id:
        ctx = int(st.get("ctx") or default_ctx)
        if wanted_ctx and wanted_ctx != ctx:
            notes.append(f"requested context {wanted_ctx} ignored: the model is already loaded with {ctx} and changing it would reload it for everyone")
        return Prepared(ctx, st.get("gpu_layers"), True, False, notes, False)
    ctx = min(int(wanted_ctx or default_ctx), cmax) if cmax else int(wanted_ctx or default_ctx)
    if state == "ready" and loaded:
        idle = time.time() - float(st.get("last_used") or 0)
        if int(st.get("active_streams") or 0) > 0:
            raise ModelError(f"{st.get('model_name') or loaded} is generating a response for another client right now. "
                             "Loading a different model would cut it off, so I won't. Use the loaded model or try again later.")
        msg = (f"The engine currently has {st.get('model_name') or loaded} loaded (last used {int(idle // 60)} min ago). "
               f"Using {model.get('name')} will UNLOAD it for everyone using this engine.")
        if not (allow_swap or (confirm_swap and confirm_swap(msg))):
            raise ModelError(msg + " Cancelled. Re-run with --allow-model-swap to permit this, or choose the loaded model.")
        notes.append("model swap approved: " + msg)
        return Prepared(ctx, None, True, False, notes, True, replaces=loaded)
    elif state in {"loading"}:
        notes.append("the engine is loading a model right now; the request will wait for it")
    return Prepared(ctx, None, True, False, notes, True)


def build_run_config(model: dict[str, Any], prepared: Prepared, settings: dict[str, Any], server: str) -> RunConfig:
    return RunConfig(
        model=model["ref"], caps=capabilities(model, server), effort=settings["effort"], thinking=settings["thinking"],
        user_max_tokens=settings.get("max_output_tokens"), ctx=prepared.ctx, gpu_layers=prepared.gpu_layers,
        pin_ctx=prepared.pin, max_steps=settings["max_steps"], retries=settings["retries"], server=server,
        show_reasoning=settings["show_reasoning"], log_level=settings["log_level"])


def describe_model(m: dict[str, Any], server: str = "", loaded_id: str | None = None) -> str:
    from .modelinfo import capabilities as caps_of
    c = caps_of(m, server)
    flags = []
    if loaded_id and m.get("ref") == f"local:{loaded_id}":
        flags.append("LOADED")
    if m.get("vision"):
        flags.append("vision")
    if c.reasoning:
        flags.append("reasoning")
    if m.get("supported") is False:
        flags.append("UNSUPPORTED")
    ctx = f"{m['context_max']:,}" if m.get("context_max") else "?"
    return f"{m.get('ref')}  |  {m.get('name')}  |  ctx max {ctx} (default {m.get('default_ctx') or '?'})  |  tools: {c.tool_calling}  |  " + (", ".join(flags) or "-")
