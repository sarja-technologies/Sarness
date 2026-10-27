# Sarness CLI: design notes and live verification

The user guide is [`cli/README.md`](../cli/README.md). This page records *how it is built to fit the engine* and
*what was verified against a real engine*, including what was **not**.

## How it uses the engine

| Need | Engine API used | Notes |
|---|---|---|
| liveness / compatibility | `GET /v1/health` (no auth) | requires `service == sarness-engine`, API major `1` |
| models | `GET /v1/agent/models` + `GET /v1/models?category=llm` | `tools` is `null` for local models → shown as *unverified*; `capabilities.reasoning`, architecture, quant come from the registry |
| what is loaded | `GET /v1/llm/status` | used to avoid evicting a loaded model |
| inference | `POST /v1/agent/completions` (SSE) | `delta{content,reasoning}`, `tool_call{index,name}` (progress only), `done{message,usage,timings,finish_reason}`, `error` |
| thinking | `params.thinking` → `enable_thinking` | the **only** reasoning control the engine forwards (local models only) |

### Engine behaviours that shaped the design

* `ensure_local()` reloads a model when `params.ctx` or `gpu_layers` differ from the loaded ones, and unloads the previous model first.
  Defaults (`ctx = default_ctx`) differ from what a user may have loaded (e.g. 32 768), so a naive client silently evicts. The CLI pins the
  loaded model's own `ctx`/`gpu_layers`, asks before unloading a different model, refuses while another client is generating,
  and re-checks before every turn.
* Tool-call arguments are assembled **by the engine**; the stream's `tool_call` events carry only a name. The CLI therefore treats the
  `done` message as the only authoritative tool-call source, and discards it when `finish_reason == "length"`.
* The engine has no reasoning-effort parameter (not even for gpt-oss). Effort levels are agent-side and labelled as such.
  *Suggested upstream change:* forward `reasoning_effort` (and `max_thinking_tokens` where supported) via `chat_template_kwargs`, and report
  `tools` support per model from the GGUF chat template, so clients need not guess.
* The registry under-reports reasoning for some models (Qwen3.5 0.8B streamed reasoning when `thinking=true` although `capabilities.reasoning` was unset).

## Live verification (2026-10-27)

Environment: Ubuntu desktop (CLI, tools), Sarness Engine 0.4.0 / API 1.1 on a DGX Spark (GB10, 121 GB unified memory)
through an SSH tunnel with key auth; token imported from the Sarness Desktop keyring (never displayed). 13 chat models were discovered.
A second profile (`rtx4070ti`, 3 models) was configured and connection-tested. Nothing was downloaded. Only two small models were loaded, by me, into free
memory, and unloaded afterwards; the Spark was left with nothing loaded, as found.

| Check | Result |
|---|---|
| `sarness setup` (SSH tunnel, keyring token, real connection test) for both servers | ✔ |
| `sarness --version / --help / models / model / config / doctor` in a fresh login shell | ✔ (`doctor`: all checks passed, 1 warning: `/usr/bin/sarness` is shadowed) |
| Real coding task, Qwen3 4B Instruct: fix `median()` bug, run `unittest`, report | ✔ 6 turns, 13.6 s; diff and test run re-verified independently |
| Same task with the tunnel **killed mid-stream** | ✔ `reconnecting (attempt 1/3)`, tunnel re-opened, task finished, tests pass |
| Oversized call on the real engine (`--max-tokens 700`, 8-function module): a ~17 000-character `edit_file` call hit `finish_reason=length` | ✔ discarded, nothing half-applied; the run later stopped itself as `blocked:loop` (the 4B model could not repair its own tests; 8 of its 44 tests failed). It did not claim success, and the run summary said so |
| Interactive REPL in a pty: `/status /effort /thinking /permissions /model`, approval prompts with diffs, Ctrl-C mid-generation | ✔ |
| Model swap safety: selecting another model while one is loaded | ✔ asked, declined, nothing unloaded; with consent the swap worked and the per-turn guard stayed quiet |
| `--thinking on/off` on the live engine (Qwen3.5 0.8B) | ✔ on: 1 402 reasoning characters streamed separately from the 34-character answer; off: 0 reasoning characters |
| `--dangerously-skip-permissions` on a real model | ✔ `mkdir`/`rm -rf build` ran with no prompt; `git push` was still gated (denied: no terminal) |

Bugs found *only* by live testing and fixed: (1) startup swap consent made the per-turn guard fire on turn 1; (2) Ctrl-C at an approval prompt was
treated as "deny" instead of aborting; (3) blocked runs did not print the independent summary; (4) stale `.pyc` after a same-size edit within one
second (commands now run with `PYTHONDONTWRITEBYTECODE=1`).

### Not verified, and why

* **Large / reasoning-capable models** (Qwen3 Coder Next 80B-A3B, Qwen3.8 27B, GPT-OSS 120B, …): not loaded. During testing the Spark was running
  other `llama-server` workloads (≈67 GB in use, ≈54 GB available, one started a minute earlier), and GB10 GPU memory is invisible to the OOM killer. Loading a 23–56 GB
  model could have disrupted them. So **high/max effort with a real reasoning model, and the default coder model, are untested**. The mechanics (thinking
  parameter, budgets, reasoning-exhaustion recovery) are covered by unit tests and by the tiny-model run above. Expect weaker small models (≤ 8B) to
  need review: they rewrite whole files and write weak tests.
* **HTTPS / certificate pinning** against a live engine (the engine serves plain HTTP). Pinning is tested against a local self-signed TLS server.
* **SSH password prompts**: the key-auth path was used; `ssh -f` keeps interactive prompts on the terminal, but this was not exercised.
* Python 3.11 (tests ran on 3.12 and 3.14).

## Reproducing

```bash
cd cli && ./install.sh && sarness setup && sarness doctor
cd cli && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,keyring]' && .venv/bin/python -m pytest tests -q   # 191 tests
```
