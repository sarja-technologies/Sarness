# Code mode: the local coding agent

Code mode is an autonomous, tool-using coding agent. It can create, read, edit, move and delete files in a
project folder, search code, run commands, start dev servers, use Git, detect failures and fix them, until
the task is done, blocked or you stop it.

## The one rule: where things run

```
 Sarness desktop (your Ubuntu machine)                       Sarness engine (GPU server)
 ┌─────────────────────────────────────────────┐             ┌──────────────────────────┐
 │ agent loop (TypeScript)  ── messages+tools ─┼────────────▶│ POST /v1/agent/completions│
 │   ▲   │                                      │◀─ tool_calls┤  stateless inference only │
 │   │   ▼ validated tool calls                 │             │  (llama.cpp / endpoint)   │
 │ native runtime (Rust) executes them HERE:    │             └──────────────────────────┘
 │ files · commands · processes · git           │
 └─────────────────────────────────────────────┘
```

* **Inference** – the engine only turns a transcript and a list of tool schemas into the model's next
  message (text and/or structured `tool_calls`). The route stores nothing and runs no tools. It needs
  engine **v0.2.0 or newer**; with an older engine Code mode says so and changes nothing.
* **Orchestration** – `desktop/src/lib/agent/loop.ts`: sends the transcript, validates and executes each
  tool call, feeds back the real result, repeats. Retries transient model/network errors, trims context,
  supports Stop and Continue, nudges a model that writes tool calls as text, and refuses to accept "done"
  after edits that were never run.
* **Execution** – `desktop/src-tauri/src/agent/`: the only code that touches your disk or spawns
  processes. Nothing is ever executed on the GPU server, and no tool silently falls back to it.

## Tools

`read_file`, `write_file`, `edit_file` (exact, unique match), `delete_path`, `move_path`, `list_dir`,
`search` (regex, .gitignore-aware), `find_files`, `run_command`, `start_process` / `process_output` /
`stop_process` / `list_processes`, `git_status`, `git_diff`, `git_log`, `git_branch`, `git_commit`,
`diagnostics`, `update_plan`. Schemas live in `src-tauri/src/agent/tooldefs.rs` and are used both to
describe the tools to the model and to validate every call (required fields, unknown fields, types).
Results are real stdout, stderr, exit codes and diffs.

## Workspace and projects

Open Folder, New Project (creates the folder) and Recent Projects are in the project selector above the
prompt box. Registering a folder is what authorises the agent for it (stored in
`~/.config/sarness/projects.json`). The active path is always shown. Each conversation is tied to a
project; the sidebar shows which. The **Project** button opens a panel with the file tree, Git status,
and diffs (also for untracked files). Conversations, plans, tool output, the model transcript and the
project are restored after a restart; a task that was running when the app closed comes back as
*Blocked* with **Continue task**.

## Permission modes

| Mode | Behaviour |
|---|---|
| **Ask** | Confirms every file change and every command. Reads inside the project are allowed. |
| **Workspace Auto** (default) | File edits and ordinary commands inside the project run immediately. Dependency installs, network access, anything outside the project, Git commits and risky commands ask first. |
| **Full Access** | Per session, behind a warning dialog; never restored after a restart. No routine prompts, but destructive (`git reset --hard`, recursive deletes of `.`/`*`/`.git`), remote (`git push`, `ssh`, publishing) and privileged (`sudo`, `curl … | sh`) actions still ask. |

Always, in every mode: `rm -rf /` / `~`, `mkfs`, `dd of=/dev/…`, `shutdown`, fork bombs are **refused**;
the project root can never be deleted; writes to `.git/` ask; credential-like files (`.env*`, `*.pem`,
`id_rsa*`, `.npmrc`, `~/.ssh`, Sarness' own config) need approval even to read and are skipped by search
and commits; commands run with API-key/token/secret environment variables removed and known token patterns
redacted from output; commands run in their own process group so Stop and timeouts kill the whole tree.

Approvals are issued by the native layer and are bound to the exact call (tool + arguments + workspace),
single use, and expire after 30 minutes. The model cannot approve its own action. Content read from files
or command output is treated as data: the system prompt says so, and the policy does not depend on what the
model or a repository claims.

**Honest limits.** File tools are strictly confined by path resolution (`..`, absolute paths and symlinks
that leave the project are "outside"). Shell commands cannot be proven contained by static analysis: the
classifier is a conservative heuristic, not an OS sandbox. Workspace Auto is "appropriate", not
"safe against a hostile model". Use Ask mode, a disposable project, or a container/VM for untrusted work.

## Models

Any chat model on the server can be used; the picker warns when tool use is unlikely or unverified (names
are only a hint). A model that never produces structured tool calls is detected and the task is marked
*Blocked* with an explanation. Larger instruction-tuned models (Qwen 2.5/3, Llama 3.1+, Mistral/Devstral,
GLM-4, gpt-oss) work best; small models write weak tests and miss requirements, so **review the diffs**:
Sarness reports real exit codes, not whether the tests are meaningful.

## Platforms

Linux is the implemented and validated target. All OS-specific behaviour (shell, process groups, killing
a process tree, environment scrubbing) is isolated in `src-tauri/src/agent/platform.rs`. The macOS path
shares the unix code and the Windows path (`cmd /C`, `taskkill`) is an untested adapter; neither is
validated.

## Development

```bash
cd desktop/src-tauri && cargo test --lib agent     # native tools, policy, processes, git
cd desktop && npx vitest run src/lib/agent          # loop, reducers, persistence (scripted models)
cd engine && .venv/bin/python -m pytest tests/test_agent_api.py
```

### End-to-end check against a real model (no GPU server needed)

The loop, tools and UI can be exercised with any OpenAI-compatible tool-calling model, e.g. a local
`llama-server --jinja` with a Qwen 2.5/3 GGUF, without touching your real connection profile:

```bash
# 1. a throwaway engine that points at the model (the agent route is stateless)
sarness-engine --data-dir /tmp/e2e-engine init --host 127.0.0.1 --port 18743 --json   # prints a token
sarness-engine --data-dir /tmp/e2e-engine serve &
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"llm_endpoints":[{"id":"loc","name":"local","base_url":"http://127.0.0.1:18090/v1","models":["qwen3-8b"]}]}' \
  http://127.0.0.1:18743/v1/settings

# 2. the desktop app with its own config/data dirs and no keyring (token in a private file)
XDG_CONFIG_HOME=/tmp/e2e/config XDG_DATA_HOME=/tmp/e2e/data XDG_CACHE_HOME=/tmp/e2e/cache \
  env -u DBUS_SESSION_BUS_ADDRESS ./desktop/src-tauri/target/release/sarness
#   /tmp/e2e/config/sarness/profiles.json -> {"profiles":[{id,name,host:"127.0.0.1",port:18743,scheme:"http",mode:"direct",ssh:null,has_token:true}],"active":id}
#   /tmp/e2e/config/sarness/secrets.json  -> {"<id>": "<token>"}   (chmod 600)
```

Use a disposable project folder, then check the result yourself (`git diff`, run the tests): the agent's
summary is evidence, not proof.
