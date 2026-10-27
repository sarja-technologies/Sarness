"""Interactive session: prompt_toolkit input (multiline, history, completion) and slash commands."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .agent import Interrupted, RunConfig, Session, SessionBlocked
from .config import (ConfigError, SAVEABLE_MODES, EFFORTS, PERMISSION_MODES, THINKING, state_dir, update_settings)
from .modelinfo import capabilities, effort_report
from .runtime import ModelError, describe_model, prepare_model, resolve_model
from .ui import Console, banner_bypass, warning_bypass

HELP = """\
Slash commands
  /help                      this list
  /model [id|name]           show/choose the model (switching is safe mid-session)
  /effort [low|medium|high|max]   show/set effort (agent-side strategy; see /status)
  /thinking [on|off|auto]    reasoning on/off for models that support it
  /permissions [mode]        ask | auto | full-access | bypass   (add --save to keep ask/auto/full-access as default)
  /status                    model, context, effort, permissions, git, session
  /plan                      show the task checklist
  /diff                      git diff of the workspace
  /compact                   archive old messages, keep a digest
  /clear                     start a fresh conversation
  /resume [id|name]          list or continue a saved session
  /rename <name>             name this session
  /exit                      quit            (Alt+Enter or Esc,Enter = newline; Enter = send; Ctrl-C cancels a running task)
"""
COMMANDS = ["/help", "/model", "/effort", "/thinking", "/permissions", "/status", "/plan", "/diff", "/compact", "/clear", "/resume", "/rename", "/exit", "/quit"]


class Repl:
    def __init__(self, session: Session, engine: Any, console: Console, models: list[dict[str, Any]], settings: dict[str, Any],
                 server: str, workspace: Path, scope_pref: str):
        self.s, self.engine, self.con, self.models = session, engine, console, models
        self.settings, self.server, self.workspace = settings, server, workspace
        self.scope = scope_pref
        self.cfg: RunConfig = session.cfg
        self.max_steps = settings["max_steps"]

    # ------------------------------------------------------------------ input
    def _make_prompt(self):
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.key_binding import KeyBindings
        except ImportError:
            return None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        hist = state_dir() / "history"
        hist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):  # noqa: ANN001
            buf = event.current_buffer
            if buf.text.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _nl(event):  # noqa: ANN001
            event.current_buffer.insert_text("\n")

        def toolbar():
            c = self.cfg
            return (f" {c.model.split(':', 1)[-1]} · effort {c.effort} · thinking {c.thinking} · {self.s.ws.mode}"
                    f" · ctx {c.ctx} · {self.workspace.name} ")
        return PromptSession(history=FileHistory(str(hist)), multiline=True, key_bindings=kb, bottom_toolbar=toolbar,
                             completer=WordCompleter(COMMANDS, sentence=True), complete_while_typing=False,
                             prompt_continuation=lambda w, n, s: "… ")

    def read(self, ps: Any) -> str | None:
        try:
            if ps:
                return ps.prompt("sarness ❯ ").strip()
            return input("\nsarness> ").strip()
        except EOFError:
            return None
        except KeyboardInterrupt:
            self.con.err(self.con.st.dim("(Ctrl-C clears the line; Ctrl-D or /exit quits)"))
            return ""

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        c = self.con
        ps = self._make_prompt()
        c.err(c.st.bold(f"Sarness CLI {__version__}") + c.st.dim(f"  ·  {self.server}  ·  {self.workspace}"))
        c.err(c.st.dim(f"model {self.cfg.model} · effort {self.cfg.effort} · permissions {self.s.ws.mode} · session {self.s.id[:8]}  ·  /help"))
        if self.s.ws.mode == "bypass":
            c.err(banner_bypass(c.st))
        while True:
            line = self.read(ps)
            if line is None:
                break
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line):
                    break
                continue
            self.task(line)
        c.err(c.st.dim(f"session {self.s.id} saved. Resume: sarness resume {self.s.name or self.s.id}"))
        return 0

    def task(self, prompt: str) -> None:
        try:
            self.s.run(self.engine, prompt, None)
        except Interrupted:
            self.con.spin.stop()
            self.con.err(self.con.st.yellow("\n■ cancelled. State is saved; any command in flight was killed. Type a new instruction, or /resume later."))
        except SessionBlocked as exc:
            self.con.spin.stop()
            self.con.err(self.con.st.red(f"\n■ stopped: {exc}"))
            self.con.summary(self.s.verification())
        except KeyboardInterrupt:  # between phases
            self.con.spin.stop()
            self.con.err(self.con.st.yellow("\n■ cancelled."))

    # ------------------------------------------------------------------ commands
    def command(self, line: str) -> bool:
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        c = self.con
        try:
            if cmd in {"/exit", "/quit"}:
                return True
            if cmd == "/help":
                c.err(HELP)
            elif cmd == "/model":
                self.cmd_model(args)
            elif cmd == "/effort":
                self.cmd_effort(args)
            elif cmd == "/thinking":
                self.cmd_thinking(args)
            elif cmd == "/permissions":
                self.cmd_permissions(args)
            elif cmd == "/status":
                self.cmd_status()
            elif cmd == "/plan":
                c.plan(self.s.ws.plan) if self.s.ws.plan else c.err("No plan yet.")
            elif cmd == "/diff":
                r = subprocess.run(["git", "diff", "--no-color"], cwd=self.workspace, capture_output=True, text=True)
                c.print_diff(r.stdout, 400) if r.stdout else c.err("No uncommitted changes (or not a git repository).")
            elif cmd == "/compact":
                n = self.s.compact_now()
                c.err(f"archived {n} old messages; a digest of files/commands/plan stays in context." if n else "Nothing to compact yet.")
            elif cmd == "/clear":
                self.s.clear()
                c.err("Conversation cleared (previous session remains resumable). New session " + self.s.id[:8])
            elif cmd == "/resume":
                self.cmd_resume(args)
            elif cmd == "/rename":
                self.s.name = "-".join(args)[:48] or self.s.name
                self.s.save()
                c.err(f"session named '{self.s.name}'")
            else:
                c.err(f"Unknown command {cmd}. /help lists them.")
        except (ConfigError, ModelError, ValueError) as exc:
            c.err(c.st.red(f"! {exc}"))
        return False

    def _remember(self, changes: dict[str, Any]) -> None:
        try:
            scope = "project" if self.scope == "project" else ("server" if set(changes) == {"model"} else "global")
            where = update_settings(changes, workspace=self.workspace, scope=scope, server=self.server)
            self.con.err(self.con.st.dim(f"(remembered in {where})"))
        except ConfigError as exc:
            self.con.err(self.con.st.yellow(f"! not saved: {exc}"))

    def cmd_model(self, args: list[str]) -> None:
        c = self.con
        engine = self.engine
        loaded = None
        try:
            loaded = engine.llm_status().get("model_id")
            self.models = engine.conn.engine.models()
        except Exception:  # noqa: BLE001 - listing is best-effort; fall back to the cached list
            pass
        if not args:
            for i, m in enumerate(self.models, 1):
                mark = "▶" if m["ref"] == self.cfg.model else " "
                c.err(f"{mark}{i:>2}. {describe_model(m, self.server, loaded)}")
            try:
                pick = c.input_fn("Number or id (Enter to keep current): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not pick:
                return
            args = [self.models[int(pick) - 1]["ref"]] if pick.isdigit() and 1 <= int(pick) <= len(self.models) else [pick]
        model = resolve_model(self.models, " ".join(args))
        if model.get("supported") is False:
            raise ModelError(f"{model['name']} is not supported by this engine: {model.get('unsupported_reason') or 'unknown reason'}")
        if model["ref"] == self.cfg.model:
            c.err("Already using " + model["ref"])
            return
        prepared = prepare_model(engine.conn.engine, model, wanted_ctx=self.settings.get("context_tokens"),
                                 confirm_swap=lambda msg: c.confirm(msg + "\nSwitch anyway?") is not False)
        old = self.cfg.model
        engine.replaces = prepared.replaces
        self.cfg.model, self.cfg.caps = model["ref"], capabilities(model, self.server)
        self.cfg.ctx, self.cfg.gpu_layers, self.cfg.pin_ctx = prepared.ctx, prepared.gpu_layers, prepared.pin
        self.s.ctx_shrink = 1.0
        self.s.messages.append({"role": "user", "content": f"[sarness] The model was switched from {old} to {model['ref']}. "
                                 "Earlier assistant turns came from the previous model; continue the same task."})
        self.s.save()
        c.err(c.st.green(f"✓ model → {model['ref']}") + c.st.dim(f"  ctx {prepared.ctx}  tools: {self.cfg.caps.tool_calling}"))
        for n in prepared.notes:
            c.err(c.st.dim("  · " + n))
        if self.cfg.caps.tools_reported is not True and self.cfg.caps.tools_probed is not True:
            c.err(c.st.yellow("  ! tool calling for this model is unverified; if it writes calls as text the session will stop and say so."))
        self._remember({"model": model["ref"]})

    def cmd_effort(self, args: list[str]) -> None:
        if args:
            if args[0] not in EFFORTS:
                raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
            self.cfg.effort = args[0]
            self._remember({"effort": args[0]})
        for line in effort_report(self.cfg.caps, self.cfg.effort, self.cfg.thinking, self.cfg.ctx, self.cfg.user_max_tokens):
            self.con.err(line)

    def cmd_thinking(self, args: list[str]) -> None:
        if args:
            if args[0] not in THINKING:
                raise ValueError("usage: /thinking on|off|auto")
            self.cfg.thinking = args[0]
            self._remember({"thinking": args[0]})
        caps = self.cfg.caps
        self.con.err(f"thinking: {self.cfg.thinking}  (model toggle: {caps.thinking_toggle})")

    def cmd_permissions(self, args: list[str]) -> None:
        c = self.con
        save = "--save" in args
        args = [a for a in args if a != "--save"]
        if not args:
            desc = {"ask": "confirm every file change and command", "auto": "workspace edits + low-risk commands; asks for installs/network/destructive",
                    "full-access": "most local dev actions without prompts; destructive/remote/privileged still ask",
                    "bypass": "skip routine approvals (explicit opt-in; hard limits remain)"}
            for m in PERMISSION_MODES:
                c.err(f"{'▶' if m == self.s.ws.mode else ' '} {m:<12} {desc[m]}")
            try:
                pick = c.input_fn("Mode (Enter to keep): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not pick:
                return
            args = [pick]
        mode = args[0]
        if mode not in PERMISSION_MODES:
            raise ValueError(f"mode must be one of {', '.join(PERMISSION_MODES)}")
        if mode == "bypass":
            c.err(warning_bypass(c.st))
            try:
                if c.input_fn("Type 'bypass' to enable for THIS session only: ").strip() != "bypass":
                    c.err("Not enabled.")
                    return
            except (EOFError, KeyboardInterrupt):
                return
        self.s.ws.mode = mode
        self.s.ws.remembered.clear()
        c.err(c.st.green(f"✓ permissions → {mode}") + c.st.dim("  (this session only)" if mode == "bypass" or not save else ""))
        if save and mode in SAVEABLE_MODES:
            self._remember({"permissions": mode})
        elif save:
            c.err(c.st.yellow("! bypass is never saved as a default"))

    def cmd_status(self) -> None:
        c, cfg = self.con, self.cfg
        used = self.s.stats
        git = subprocess.run(["git", "status", "--short", "--branch"], cwd=self.workspace, capture_output=True, text=True).stdout.strip().splitlines()
        rows = [("server", self.server), ("workspace", str(self.workspace)), ("model", cfg.model),
                ("context window", f"{cfg.ctx} tokens" + (" (assumed)" if not cfg.pin_ctx and not cfg.caps.local else "")),
                ("tool calling", cfg.caps.tool_calling), ("effort", cfg.effort), ("thinking", f"{cfg.thinking}  [{cfg.caps.thinking_toggle}]"),
                ("native reasoning effort", "unsupported by engine 0.4.0 (effort = agent-side strategy)"),
                ("permissions", self.s.ws.mode), ("session", f"{self.s.id} ({self.s.name or 'unnamed'}) status={self.s.status}"),
                ("usage", f"{used.turns} turns, {used.prompt_tokens} prompt / {used.completion_tokens} output tokens, {used.tools_run} tool runs"),
                ("git", git[0] if git else "not a git repository")]
        for k, v in rows:
            c.err(f"  {c.st.dim(k.ljust(24))}{v}")
        for line in git[1:8]:
            c.err(c.st.dim("                          " + line))

    def cmd_resume(self, args: list[str]) -> None:
        c = self.con
        if not args:
            for s in Session.list_sessions(15):
                c.err(f"  {s['id'][:8]}  {s['status']:<22} {s['name'] or '-':<30} {s['workspace']}")
            c.err(c.st.dim("  /resume <id-prefix|name>"))
            return
        sid = Session.find(args[0])
        new = Session.load(sid, self.s.ws, self.cfg, self.con)
        new.swap_confirm = self.s.swap_confirm
        self.s = new
        c.err(c.st.green(f"✓ resumed {new.name or new.id[:8]}") + c.st.dim(f"  ({len(new.messages)} messages, status {new.status}). Send an instruction to continue."))
