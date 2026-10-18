//! Structured tool definitions (OpenAI function-calling schema). The same schemas are sent
//! to the model and used to validate every call before anything runs.

use serde_json::{json, Value};

pub struct ToolDef {
    pub name: &'static str,
    pub description: &'static str,
    pub parameters: Value,
}

fn obj(props: Value, required: &[&str]) -> Value {
    json!({"type": "object", "properties": props, "required": required, "additionalProperties": false})
}

pub fn all() -> Vec<ToolDef> {
    let s = |d: &str| json!({"type": "string", "description": d});
    let i = |d: &str| json!({"type": "integer", "description": d});
    let b = |d: &str| json!({"type": "boolean", "description": d});
    vec![
        ToolDef { name: "read_file", description: "Read a text file in the project (relative path). Returns the content with a header giving the line range. Use offset/limit for large files.",
            parameters: obj(json!({"path": s("File path relative to the project root"), "offset": i("First line to read (1-based)"), "limit": i("Maximum lines (default 2000)")}), &["path"]) },
        ToolDef { name: "write_file", description: "Create a file or completely replace its content. Parent folders are created. Prefer edit_file for changes to existing files.",
            parameters: obj(json!({"path": s("File path relative to the project root"), "content": s("Full new file content")}), &["path", "content"]) },
        ToolDef { name: "edit_file", description: "Replace exact text in an existing file. old_string must match exactly once (unless replace_all). Read the file first and copy the text precisely.",
            parameters: obj(json!({"path": s("File path"), "old_string": s("Exact text to replace"), "new_string": s("Replacement text"), "replace_all": b("Replace every occurrence")}), &["path", "old_string", "new_string"]) },
        ToolDef { name: "delete_path", description: "Delete a file or folder inside the project.",
            parameters: obj(json!({"path": s("Path to delete"), "recursive": b("Required to delete a non-empty folder")}), &["path"]) },
        ToolDef { name: "move_path", description: "Move or rename a file or folder inside the project.",
            parameters: obj(json!({"from": s("Existing path"), "to": s("New path"), "overwrite": b("Replace the destination if it exists")}), &["from", "to"]) },
        ToolDef { name: "list_dir", description: "List a folder (folders first). depth 1-4.",
            parameters: obj(json!({"path": s("Folder, default project root"), "depth": i("Levels to descend, default 1")}), &[]) },
        ToolDef { name: "search", description: "Search file contents with a regular expression (respects .gitignore, skips build folders and credential-like files).",
            parameters: obj(json!({"pattern": s("Regular expression"), "path": s("Folder or file to search, default project root"), "glob": s("Only files matching this glob, e.g. *.ts"), "case_insensitive": b("Ignore case"), "max_results": i("Default 100")}), &["pattern"]) },
        ToolDef { name: "find_files", description: "Find files by glob pattern, e.g. **/*.test.ts.",
            parameters: obj(json!({"pattern": s("Glob pattern"), "path": s("Folder, default project root"), "max_results": i("Default 200")}), &["pattern"]) },
        ToolDef { name: "run_command", description: "Run a shell command on the LOCAL machine in the project folder and wait for it. Returns the real exit code, stdout and stderr. Use for installs, builds, tests, scripts. Not for servers that never exit — use start_process.",
            parameters: obj(json!({"command": s("Shell command"), "cwd": s("Working directory relative to the project root"), "timeout_s": i("Timeout in seconds (default 120, max 1800)")}), &["command"]) },
        ToolDef { name: "start_process", description: "Start a long-running local process (dev server, watcher) in the background and return its first output. It is stopped when the task ends.",
            parameters: obj(json!({"command": s("Shell command"), "cwd": s("Working directory"), "wait_ms": i("How long to wait for initial output (default 3000)")}), &["command"]) },
        ToolDef { name: "process_output", description: "Read recent output and status of a background process.",
            parameters: obj(json!({"id": s("Process id from start_process"), "tail_bytes": i("Default 8000")}), &["id"]) },
        ToolDef { name: "stop_process", description: "Stop a background process.", parameters: obj(json!({"id": s("Process id")}), &["id"]) },
        ToolDef { name: "list_processes", description: "List background processes started in this session.", parameters: obj(json!({}), &[]) },
        ToolDef { name: "git_status", description: "Git status of the project (branch and changed files).", parameters: obj(json!({}), &[]) },
        ToolDef { name: "git_diff", description: "Git diff of uncommitted changes (also lists untracked files).",
            parameters: obj(json!({"path": s("Limit to this path"), "staged": b("Show staged changes")}), &[]) },
        ToolDef { name: "git_log", description: "Recent commits.", parameters: obj(json!({"count": i("Default 15")}), &[]) },
        ToolDef { name: "git_branch", description: "List, create or switch Git branches.",
            parameters: obj(json!({"action": json!({"type": "string", "enum": ["list", "create", "switch"]}), "name": s("Branch name for create/switch")}), &["action"]) },
        ToolDef { name: "git_commit", description: "Create a commit. Only use when the user asked you to commit.",
            parameters: obj(json!({"message": s("Commit message"), "paths": json!({"type": "array", "items": {"type": "string"}, "description": "Files to stage; default all changes"})}), &["message"]) },
        ToolDef { name: "diagnostics", description: "Report the execution environment: host, OS, tool versions, workspace path, git state, background processes.", parameters: obj(json!({}), &[]) },
        ToolDef { name: "update_plan", description: "Record or update a short task plan shown to the user. Use for substantial tasks.",
            parameters: obj(json!({"steps": json!({"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}}, "required": ["text", "status"]}})}), &["steps"]) },
    ]
}

pub fn lookup(name: &str) -> Option<ToolDef> {
    all().into_iter().find(|t| t.name == name)
}

pub fn names() -> Vec<&'static str> {
    all().iter().map(|t| t.name).collect()
}

pub fn openai_tools() -> Value {
    Value::Array(all().into_iter().map(|t| json!({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}})).collect())
}

fn type_ok(schema: &Value, v: &Value) -> bool {
    match schema.get("type").and_then(Value::as_str) {
        Some("string") => v.is_string() && schema.get("enum").and_then(Value::as_array).map(|e| e.contains(v)).unwrap_or(true),
        Some("integer") => v.is_i64() || v.is_u64(),
        Some("boolean") => v.is_boolean(),
        Some("array") => v.as_array().is_some_and(|a| a.iter().all(|x| schema.get("items").map(|s| type_ok(s, x)).unwrap_or(true))),
        Some("object") => v.is_object(),
        _ => true,
    }
}

/// Checks required properties, unknown properties and basic types.
pub fn validate_args(def: &ToolDef, args: &Value) -> Result<(), String> {
    let args = if args.is_null() { json!({}) } else { args.clone() };
    let Some(map) = args.as_object() else { return Err("arguments must be a JSON object".into()) };
    let props = def.parameters["properties"].as_object().cloned().unwrap_or_default();
    for r in def.parameters["required"].as_array().into_iter().flatten().filter_map(Value::as_str) {
        match map.get(r) {
            None | Some(Value::Null) => return Err(format!("missing required argument `{r}`")),
            _ => {}
        }
    }
    for (k, v) in map {
        let Some(schema) = props.get(k) else { return Err(format!("unknown argument `{k}` (allowed: {})", props.keys().cloned().collect::<Vec<_>>().join(", "))) };
        if !type_ok(schema, v) {
            return Err(format!("argument `{k}` has the wrong type (expected {})", schema["type"].as_str().unwrap_or("?")));
        }
    }
    Ok(())
}

pub fn approval_title(tool: &str, a: &Value) -> String {
    let s = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("");
    match tool {
        "write_file" => format!("Write {}", s("path")),
        "edit_file" => format!("Edit {}", s("path")),
        "delete_path" => format!("Delete {}", s("path")),
        "move_path" => format!("Move {} → {}", s("from"), s("to")),
        "run_command" => "Run command".into(),
        "start_process" => "Start background process".into(),
        "read_file" => format!("Read {}", s("path")),
        "git_commit" => "Commit changes".into(),
        other => other.replace('_', " "),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation() {
        let d = lookup("edit_file").unwrap();
        assert!(validate_args(&d, &json!({"path": "a", "old_string": "x", "new_string": "y"})).is_ok());
        assert!(validate_args(&d, &json!({"path": "a"})).unwrap_err().contains("old_string"));
        assert!(validate_args(&d, &json!({"path": 3, "old_string": "x", "new_string": "y"})).unwrap_err().contains("wrong type"));
        assert!(validate_args(&d, &json!({"path": "a", "old_string": "x", "new_string": "y", "bogus": 1})).unwrap_err().contains("unknown argument"));
        assert!(validate_args(&lookup("git_branch").unwrap(), &json!({"action": "nuke"})).is_err());
    }
}
