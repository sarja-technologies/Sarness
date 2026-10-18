//! Tauri command surface of the local agent. Thin wrappers over `Engine`.

use super::exec::{Emit, OutputEvent};
use super::{fstools, git, projects, tooldefs, workspace::Workspace, Engine, ToolRequest, ToolResponse};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tauri::{AppHandle, Emitter, State};

pub struct AgentState {
    pub engine: Arc<Engine>,
    pub projects_file: PathBuf,
}

impl AgentState {
    pub fn new(projects_file: PathBuf) -> Self {
        let engine = Arc::new(Engine::new());
        for p in projects::load(&projects_file) {
            let _ = engine.register_workspace(&p.path); // folders that vanished are skipped
        }
        Self { engine, projects_file }
    }
}

#[tauri::command]
pub fn agent_tool_defs() -> Value {
    tooldefs::openai_tools()
}

#[tauri::command]
pub async fn agent_call(app: AppHandle, st: State<'_, AgentState>, req: ToolRequest) -> Result<ToolResponse, String> {
    let emit: Emit = Arc::new(move |ev: OutputEvent| {
        let _ = app.emit("agent://output", ev);
    });
    Ok(st.engine.call(req, Some(emit)).await)
}

#[tauri::command]
pub async fn agent_cancel(st: State<'_, AgentState>, run_id: String) -> Result<(), String> {
    st.engine.cancel(&run_id).await;
    Ok(())
}

#[tauri::command]
pub async fn agent_end_run(st: State<'_, AgentState>, run_id: String) -> Result<usize, String> {
    Ok(st.engine.end_run(&run_id).await)
}

#[tauri::command]
pub fn agent_reset_run(st: State<'_, AgentState>, run_id: String) {
    st.engine.reset_run(&run_id);
}

#[tauri::command]
pub fn projects_list(st: State<'_, AgentState>) -> Vec<projects::Project> {
    projects::load(&st.projects_file)
        .into_iter()
        .filter(|p| Path::new(&p.path).is_dir() && !Path::new(&p.path).components().any(|c| c.as_os_str() == ".git"))
        .collect()
}

#[tauri::command]
pub fn projects_add(st: State<'_, AgentState>, path: String) -> Result<Vec<projects::Project>, String> {
    let list = projects::add(&st.projects_file, Path::new(&path))?;
    st.engine.register_workspace(&path)?;
    Ok(list)
}

#[tauri::command]
pub fn projects_remove(st: State<'_, AgentState>, path: String) -> Result<Vec<projects::Project>, String> {
    projects::remove(&st.projects_file, &path)
}

#[tauri::command]
pub fn project_create(st: State<'_, AgentState>, parent: String, name: String) -> Result<String, String> {
    let dir = projects::create(&st.projects_file, Path::new(&parent), &name)?;
    st.engine.register_workspace(&dir.to_string_lossy())?;
    Ok(dir.display().to_string())
}

fn open_registered(st: &AgentState, path: &str) -> Result<Workspace, String> {
    let ws = Workspace::open(path)?;
    st.engine.register_workspace(path).ok(); // already authorised by the user via projects_add
    Ok(ws)
}

#[tauri::command]
pub fn project_tree(st: State<'_, AgentState>, path: String, depth: Option<usize>) -> Result<Value, String> {
    let ws = open_registered(&st, &path)?;
    Ok(fstools::tree(&ws.root, depth.unwrap_or(3).min(5)))
}

/// Git state for the UI: whether it is a repo, the branch and changed files.
#[tauri::command]
pub async fn project_git(st: State<'_, AgentState>, path: String) -> Result<Value, String> {
    let ws = open_registered(&st, &path)?;
    let ctl = super::exec::RunCtl::new();
    match git(&ws, &["status", "--porcelain=v1", "--branch"], &ctl).await {
        Err(_) => Ok(json!({"repo": false})),
        Ok(o) => {
            let mut branch = String::new();
            let mut files = vec![];
            for l in o.content.lines() {
                if let Some(b) = l.strip_prefix("## ") {
                    branch = b.to_string();
                } else if l.len() > 3 {
                    files.push(json!({"status": l[..2].trim(), "path": l[3..]}));
                }
            }
            Ok(json!({"repo": true, "branch": branch, "files": files}))
        }
    }
}

/// Unified diff for the UI (one file or everything, including untracked files as additions).
#[tauri::command]
pub async fn project_diff(st: State<'_, AgentState>, path: String, file: Option<String>) -> Result<String, String> {
    let ws = open_registered(&st, &path)?;
    let ctl = super::exec::RunCtl::new();
    let mut args = vec!["diff", "--no-color", "HEAD"];
    let f;
    if let Some(file) = file {
        ws.resolve(&file)?;
        f = file;
        args.push("--");
        args.push(&f);
    }
    let mut out = match git(&ws, &args, &ctl).await {
        Ok(o) => o.content,
        Err(_) => git(&ws, &["diff", "--no-color"], &ctl).await.map(|o| o.content).unwrap_or_default(), // no commits yet
    };
    let untracked = git(&ws, &["ls-files", "--others", "--exclude-standard"], &ctl).await.map(|o| o.content).unwrap_or_default();
    for u in untracked.lines().take(50) {
        if let Ok(r) = ws.resolve(u) {
            if let Ok(text) = std::fs::read_to_string(&r.path) {
                if !super::workspace::is_sensitive(&r.path) {
                    out.push_str(&fstools::unified_diff(u, "", &text));
                }
            }
        }
    }
    Ok(out)
}

/// Before/after record of the project's files (size + mtime), used for the task summary.
#[tauri::command]
pub fn project_snapshot(st: State<'_, AgentState>, path: String) -> Result<Value, String> {
    let ws = open_registered(&st, &path)?;
    Ok(fstools::snapshot(&ws.root, 20_000))
}
