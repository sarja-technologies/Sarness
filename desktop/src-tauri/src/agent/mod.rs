//! Native local coding-agent runtime.
//!
//! Architecture: the *model* runs on the Sarness inference server and only ever sees text.
//! Everything in this module executes on the machine running the desktop app: file access,
//! commands, processes and Git, all scoped to a user-authorised workspace and gated by the
//! permission policy. The orchestration loop lives in the frontend and calls `Engine::call`.

pub mod commands;
pub mod exec;
pub mod fstools;
pub mod platform;
pub mod policy;
pub mod projects;
pub mod tooldefs;
pub mod workspace;

#[cfg(test)]
mod tests;

use exec::{Emit, Processes, RunCtl};
use fstools::ToolOut;
use policy::{Mode, Risk, Verdict};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use workspace::Workspace;

#[derive(Deserialize, Debug, Clone)]
pub struct ToolRequest {
    pub workspace: String,
    pub run_id: String,
    pub call_id: String,
    pub tool: String,
    #[serde(default)]
    pub args: Value,
    pub mode: Mode,
    #[serde(default)]
    pub approval_id: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum ToolResponse {
    /// The tool ran; `content` is exactly what the model is shown.
    Ok { content: String, ui: Value },
    /// The tool ran and failed (or was invalid); the model gets the error and can recover.
    Error { content: String, ui: Value },
    NeedsApproval { approval_id: String, risk: Risk, title: String, reason: String, preview: Value },
    /// Refused by policy or by the user; never executed.
    Denied { content: String },
}

pub struct Engine {
    roots: Mutex<HashSet<PathBuf>>,
    approvals: Mutex<HashMap<String, (String, Instant)>>,
    runs: Mutex<HashMap<String, Arc<RunCtl>>>,
    pub procs: Processes,
}

const APPROVAL_TTL: Duration = Duration::from_secs(30 * 60);

impl Default for Engine {
    fn default() -> Self {
        Self::new()
    }
}

impl Engine {
    pub fn new() -> Self {
        Self { roots: Mutex::default(), approvals: Mutex::default(), runs: Mutex::default(), procs: Processes::default() }
    }

    /// Authorises a folder as a workspace (the user chose it). Returns the canonical path.
    pub fn register_workspace(&self, path: &str) -> Result<PathBuf, String> {
        let ws = Workspace::open(path)?;
        self.roots.lock().unwrap().insert(ws.root.clone());
        Ok(ws.root)
    }

    fn ctl(&self, run_id: &str) -> Arc<RunCtl> {
        self.runs.lock().unwrap().entry(run_id.to_string()).or_insert_with(RunCtl::new).clone()
    }

    /// Stops the run immediately: running commands and its background processes are killed.
    pub async fn cancel(&self, run_id: &str) {
        let ctl = self.ctl(run_id);
        let _ = ctl.cancel_tx.send(true);
        let pids: Vec<u32> = ctl.pids.lock().unwrap().clone();
        for p in pids {
            platform::kill_tree(p).await;
        }
        self.procs.stop_run(run_id).await;
    }

    /// Called when a run finishes: background processes started by it are stopped.
    pub async fn end_run(&self, run_id: &str) -> usize {
        let n = self.procs.stop_run(run_id).await;
        self.runs.lock().unwrap().remove(run_id);
        n
    }

    /// Re-arms a run id after a cancel (Continue task).
    pub fn reset_run(&self, run_id: &str) {
        self.runs.lock().unwrap().remove(run_id);
    }

    pub async fn shutdown(&self) {
        self.procs.stop_all().await;
    }

    fn fingerprint(req: &ToolRequest, root: &std::path::Path) -> String {
        let mut h = Sha256::new();
        h.update(req.tool.as_bytes());
        h.update(root.to_string_lossy().as_bytes());
        h.update(serde_json::to_vec(&req.args).unwrap_or_default());
        hex::encode(h.finalize())
    }

    pub async fn call(&self, req: ToolRequest, emit: Option<Emit>) -> ToolResponse {
        let ws = match Workspace::open(&req.workspace) {
            Ok(w) => w,
            Err(e) => return ToolResponse::Error { content: e, ui: Value::Null },
        };
        if !self.roots.lock().unwrap().contains(&ws.root) {
            return ToolResponse::Denied { content: format!("{} is not an authorised project folder. Open it as a project first.", ws.root.display()) };
        }
        let ctl = self.ctl(&req.run_id);
        if ctl.cancelled() {
            return ToolResponse::Denied { content: "The run was cancelled by the user.".into() };
        }
        let def = match tooldefs::lookup(&req.tool) {
            Some(d) => d,
            None => {
                return ToolResponse::Error {
                    content: format!("Unknown tool `{}`. Available tools: {}.", req.tool, tooldefs::names().join(", ")),
                    ui: Value::Null,
                }
            }
        };
        if let Err(e) = tooldefs::validate_args(&def, &req.args) {
            return ToolResponse::Error { content: format!("Invalid arguments for `{}`: {e}", req.tool), ui: Value::Null };
        }
        let verdict = match policy::assess(&req.tool, &req.args, &ws, req.mode) {
            Ok(v) => v,
            Err(e) => return ToolResponse::Error { content: e, ui: Value::Null },
        };
        match verdict {
            Verdict::Deny(why) => return ToolResponse::Denied { content: why },
            Verdict::Allow => {}
            Verdict::Ask { risk, reason } => {
                let fp = Self::fingerprint(&req, &ws.root);
                let granted = req.approval_id.as_ref().is_some_and(|id| {
                    let mut m = self.approvals.lock().unwrap();
                    m.retain(|_, (_, t)| t.elapsed() < APPROVAL_TTL);
                    match m.get(id) {
                        Some((f, _)) if *f == fp => {
                            m.remove(id);
                            true
                        }
                        _ => false,
                    }
                });
                if !granted {
                    let id = uuid::Uuid::new_v4().to_string();
                    self.approvals.lock().unwrap().insert(id.clone(), (fp, Instant::now()));
                    return ToolResponse::NeedsApproval {
                        approval_id: id,
                        risk,
                        title: tooldefs::approval_title(&req.tool, &req.args),
                        reason,
                        preview: self.preview(&req, &ws),
                    };
                }
            }
        }
        match self.execute(&req, &ws, &ctl, emit).await {
            Ok(o) => ToolResponse::Ok { content: o.content, ui: o.ui },
            Err(e) => ToolResponse::Error { content: e, ui: Value::Null },
        }
    }

    fn preview(&self, req: &ToolRequest, ws: &Workspace) -> Value {
        let s = |k: &str| req.args.get(k).and_then(Value::as_str).unwrap_or("");
        let b = |k: &str| req.args.get(k).and_then(Value::as_bool).unwrap_or(false);
        match req.tool.as_str() {
            "write_file" => fstools::preview_write(ws, s("path"), s("content")).unwrap_or(Value::Null),
            "edit_file" => fstools::preview_edit(ws, s("path"), s("old_string"), s("new_string"), b("replace_all")).unwrap_or(Value::Null),
            "run_command" | "start_process" => json!({"kind": "command", "command": s("command"), "cwd": s("cwd")}),
            "delete_path" => json!({"kind": "delete", "path": s("path")}),
            "move_path" => json!({"kind": "move", "from": s("from"), "to": s("to")}),
            "git_commit" => json!({"kind": "commit", "message": s("message")}),
            _ => Value::Null,
        }
    }

    async fn execute(&self, req: &ToolRequest, ws: &Workspace, ctl: &Arc<RunCtl>, emit: Option<Emit>) -> Result<ToolOut, String> {
        let a = &req.args;
        let s = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let b = |k: &str| a.get(k).and_then(Value::as_bool).unwrap_or(false);
        let n = |k: &str, d: u64| a.get(k).and_then(Value::as_u64).unwrap_or(d);
        match req.tool.as_str() {
            "read_file" => fstools::read_file(ws, &s("path"), n("offset", 1) as usize, n("limit", 2000) as usize),
            "write_file" => fstools::write_file(ws, &s("path"), &s("content")),
            "edit_file" => fstools::edit_file(ws, &s("path"), &s("old_string"), &s("new_string"), b("replace_all")),
            "delete_path" => fstools::delete_path(ws, &s("path"), b("recursive")),
            "move_path" => fstools::move_path(ws, &s("from"), &s("to"), b("overwrite")),
            "list_dir" => fstools::list_dir(ws, &if s("path").is_empty() { ".".into() } else { s("path") }, n("depth", 1) as usize),
            "search" => fstools::search(ws, &s("pattern"), &s("path"), a.get("glob").and_then(Value::as_str), b("case_insensitive"), n("max_results", 100).min(500) as usize),
            "find_files" => fstools::find_files(ws, &s("pattern"), &s("path"), n("max_results", 200).min(1000) as usize),
            "run_command" => self.run_command(req, ws, ctl, emit).await,
            "start_process" => self.start_process(req, ws).await,
            "process_output" => {
                let p = self.procs.get(&s("id")).ok_or_else(|| format!("no process {}", s("id")))?;
                Ok(ToolOut::text(Self::proc_report(&p, n("tail_bytes", 8000) as usize)))
            }
            "stop_process" => {
                if self.procs.stop(&s("id")).await {
                    Ok(ToolOut::text(format!("Stopped {}", s("id"))))
                } else {
                    Err(format!("no process {}", s("id")))
                }
            }
            "list_processes" => {
                let list = self.procs.list();
                if list.is_empty() {
                    return Ok(ToolOut::text("No background processes."));
                }
                Ok(ToolOut::text(list.iter().map(|p| format!("{} · {} · {}", p.id, p.command, Self::proc_state(p))).collect::<Vec<_>>().join("\n")))
            }
            "git_status" => git(ws, &["status", "--short", "--branch"], ctl).await,
            "git_diff" => {
                let mut args = vec!["diff", "--no-color"];
                if b("staged") {
                    args.push("--cached");
                }
                let path = s("path");
                if !path.is_empty() {
                    ws.resolve(&path)?;
                    args.push("--");
                    args.push(&path);
                }
                let mut out = git(ws, &args, ctl).await?;
                let untracked = git(ws, &["ls-files", "--others", "--exclude-standard"], ctl).await.map(|o| o.content).unwrap_or_default();
                if !untracked.trim().is_empty() {
                    out.content.push_str(&format!("\n\nUntracked files (not in the diff):\n{}", untracked.trim()));
                }
                if out.content.trim().is_empty() {
                    out.content = "No changes.".into();
                }
                Ok(out)
            }
            "git_log" => git(ws, &["log", "--oneline", "--decorate", "-n", &n("count", 15).min(100).to_string()], ctl).await,
            "git_branch" => {
                let name = s("name");
                let valid = !name.is_empty() && !name.starts_with('-') && name.chars().all(|c| c.is_ascii_alphanumeric() || "._/-".contains(c));
                match s("action").as_str() {
                    "list" => git(ws, &["branch", "--all", "--list"], ctl).await,
                    "create" if valid => git(ws, &["switch", "-c", &name], ctl).await,
                    "switch" if valid => git(ws, &["switch", &name], ctl).await,
                    _ => Err("action must be list, create or switch; name may only contain letters, digits, . _ / -".into()),
                }
            }
            "git_commit" => self.git_commit(req, ws, ctl).await,
            "diagnostics" => self.diagnostics(ws, ctl).await,
            "update_plan" => {
                let steps = a.get("steps").cloned().unwrap_or(json!([]));
                Ok(ToolOut::with("Plan updated.", json!({"kind": "plan", "steps": steps})))
            }
            other => Err(format!("tool `{other}` is not implemented")),
        }
    }

    async fn run_command(&self, req: &ToolRequest, ws: &Workspace, ctl: &Arc<RunCtl>, emit: Option<Emit>) -> Result<ToolOut, String> {
        let command = req.args["command"].as_str().unwrap_or("").to_string();
        let timeout = req.args.get("timeout_s").and_then(Value::as_u64).unwrap_or(120).clamp(1, 1800);
        let cwd = match req.args.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()) {
            Some(c) => ws.resolve(c)?.path,
            None => ws.root.clone(),
        };
        if !cwd.is_dir() {
            return Err(format!("working directory {} does not exist", cwd.display()));
        }
        let cmd = platform::shell_command(&command, &cwd);
        let r = exec::run(cmd, Duration::from_secs(timeout), ctl, &req.run_id, &req.call_id, emit).await?;
        let mut text = format!("$ {command}\n");
        if r.cancelled {
            text.push_str("[cancelled by the user; the process was terminated]\n");
        } else if r.timed_out {
            text.push_str(&format!("[timed out after {timeout}s and was killed. For servers or watchers use start_process instead.]\n"));
        } else {
            text.push_str(&format!("exit code: {}   ({:.1}s)\n", r.exit_code.map(|c| c.to_string()).unwrap_or_else(|| "signal".into()), r.duration_ms as f64 / 1000.0));
        }
        if !r.stdout.trim().is_empty() {
            text.push_str(&format!("--- stdout ---\n{}\n", r.stdout.trim_end()));
        }
        if !r.stderr.trim().is_empty() {
            text.push_str(&format!("--- stderr ---\n{}\n", r.stderr.trim_end()));
        }
        Ok(ToolOut::with(
            text,
            json!({"kind": "command", "command": command, "cwd": ws.rel(&cwd), "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr,
                   "timed_out": r.timed_out, "cancelled": r.cancelled, "duration_ms": r.duration_ms, "truncated": r.truncated}),
        ))
    }

    fn proc_state(p: &exec::Proc) -> String {
        match *p.exit.lock().unwrap() {
            None => format!("running {}s", p.started.elapsed().as_secs()),
            Some(Some(c)) => format!("exited with code {c}"),
            Some(None) => "terminated by signal".into(),
        }
    }

    fn proc_report(p: &exec::Proc, tail: usize) -> String {
        format!("{} · {} · {}\n--- output ---\n{}", p.id, p.command, Self::proc_state(p), exec::render_proc(p, tail))
    }

    async fn start_process(&self, req: &ToolRequest, ws: &Workspace) -> Result<ToolOut, String> {
        let command = req.args["command"].as_str().unwrap_or("").to_string();
        let cwd = match req.args.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty()) {
            Some(c) => ws.resolve(c)?.path,
            None => ws.root.clone(),
        };
        let p = self.procs.start(platform::shell_command(&command, &cwd), &req.run_id, &command)?;
        let wait = req.args.get("wait_ms").and_then(Value::as_u64).unwrap_or(3000).min(30_000);
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_millis(wait) && p.exit.lock().unwrap().is_none() {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let report = Self::proc_report(&p, 6000);
        Ok(ToolOut::with(
            format!("Started background process {}. It is stopped automatically when this task ends.\n{report}", p.id),
            json!({"kind": "process", "id": p.id, "command": command}),
        ))
    }

    async fn git_commit(&self, req: &ToolRequest, ws: &Workspace, ctl: &Arc<RunCtl>) -> Result<ToolOut, String> {
        let msg = req.args["message"].as_str().unwrap_or("").trim().to_string();
        if msg.is_empty() {
            return Err("commit message must not be empty".into());
        }
        match req.args.get("paths").and_then(Value::as_array) {
            Some(ps) if !ps.is_empty() => {
                let mut v: Vec<String> = vec!["add".into(), "--".into()];
                for p in ps {
                    let p = p.as_str().ok_or("paths must be strings")?;
                    let r = ws.resolve(p)?;
                    if !r.inside {
                        return Err(format!("{p} is outside the project"));
                    }
                    v.push(ws.rel(&r.path));
                }
                let refs: Vec<&str> = v.iter().map(String::as_str).collect();
                git(ws, &refs, ctl).await?;
            }
            _ => {
                git(ws, &["add", "-A"], ctl).await?;
            }
        }
        let staged = git(ws, &["diff", "--cached", "--name-only"], ctl).await?.content;
        let bad: Vec<&str> = staged.lines().filter(|l| workspace::is_sensitive(std::path::Path::new(l))).collect();
        if !bad.is_empty() {
            let _ = git(ws, &["reset", "--quiet"], ctl).await;
            return Err(format!("refusing to commit credential-like files: {}. They were unstaged; add them to .gitignore.", bad.join(", ")));
        }
        if staged.trim().is_empty() {
            return Err("nothing to commit".into());
        }
        let out = git(ws, &["commit", "-m", &msg], ctl).await?;
        Ok(ToolOut::with(out.content.clone(), json!({"kind": "commit", "message": msg, "files": staged.lines().collect::<Vec<_>>()})))
    }

    async fn diagnostics(&self, ws: &Workspace, ctl: &Arc<RunCtl>) -> Result<ToolOut, String> {
        let host = std::fs::read_to_string("/etc/hostname").unwrap_or_default().trim().to_string();
        let mut lines = vec![
            format!("execution host: {} ({}/{}) — this is the machine running Sarness; the inference server never runs tools", if host.is_empty() { "unknown".into() } else { host }, platform::os_name(), std::env::consts::ARCH),
            format!("user: {}", std::env::var("USER").unwrap_or_default()),
            format!("workspace: {}", ws.root.display()),
        ];
        for (prog, arg) in [("git", "--version"), ("node", "--version"), ("npm", "--version"), ("pnpm", "--version"), ("python3", "--version"), ("pip3", "--version"), ("cargo", "--version"), ("rustc", "--version"), ("go", "version"), ("make", "--version")] {
            let cmd = platform::program_command(prog, &[arg], &ws.root);
            let v = match exec::run(cmd, Duration::from_secs(5), ctl, "diag", "diag", None).await {
                Ok(r) if r.exit_code == Some(0) => r.stdout.lines().next().unwrap_or("").to_string(),
                _ => "not found".into(),
            };
            lines.push(format!("{prog}: {v}"));
        }
        let branch = git(ws, &["status", "--short", "--branch"], ctl).await.map(|o| o.content.lines().next().unwrap_or("").to_string()).unwrap_or_else(|_| "not a git repository".into());
        lines.push(format!("git: {branch}"));
        let procs = self.procs.list();
        lines.push(format!("background processes: {}", if procs.is_empty() { "none".into() } else { procs.iter().map(|p| format!("{} ({})", p.id, Self::proc_state(p))).collect::<Vec<_>>().join(", ") }));
        Ok(ToolOut::text(lines.join("\n")))
    }
}

pub async fn git(ws: &Workspace, args: &[&str], ctl: &Arc<RunCtl>) -> Result<ToolOut, String> {
    let mut full = vec!["-c", "core.quotepath=off", "-c", "color.ui=never"];
    full.extend_from_slice(args);
    let cmd = platform::program_command("git", &full, &ws.root);
    let r = exec::run(cmd, Duration::from_secs(60), ctl, "git", "git", None).await?;
    if r.exit_code != Some(0) {
        let msg = if r.stderr.trim().is_empty() { r.stdout.clone() } else { r.stderr.clone() };
        return Err(format!("git {} failed: {}", args.join(" "), msg.trim()));
    }
    let mut c = r.stdout;
    if c.len() > fstools::MAX_OUT_CHARS {
        let mut cut = fstools::MAX_OUT_CHARS;
        while !c.is_char_boundary(cut) {
            cut -= 1;
        }
        c.truncate(cut);
        c.push_str("\n…[truncated]");
    }
    Ok(ToolOut::text(c))
}
