//! File-system tools. Every path is resolved through `Workspace` first; the permission
//! decision itself is made earlier by `policy`, so these functions just do the work and
//! report exactly what changed.

use super::workspace::{is_sensitive, Workspace};
use serde_json::{json, Value};
use similar::TextDiff;
use std::fs;
use std::path::Path;

pub struct ToolOut {
    pub content: String,
    pub ui: Value,
}

impl ToolOut {
    pub fn text(s: impl Into<String>) -> Self {
        Self { content: s.into(), ui: Value::Null }
    }
    pub fn with(content: impl Into<String>, ui: Value) -> Self {
        Self { content: content.into(), ui }
    }
}

pub const MAX_READ_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_OUT_CHARS: usize = 60_000;
const SKIP_DIRS: &[&str] = &[".git", "node_modules", "target", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".svelte-kit", ".cache"];

pub fn unified_diff(path: &str, old: &str, new: &str) -> String {
    TextDiff::from_lines(old, new)
        .unified_diff()
        .context_radius(3)
        .header(&format!("a/{path}"), &format!("b/{path}"))
        .to_string()
}

fn diff_stats(old: &str, new: &str) -> (usize, usize) {
    let d = TextDiff::from_lines(old, new);
    let (mut a, mut r) = (0, 0);
    for c in d.iter_all_changes() {
        match c.tag() {
            similar::ChangeTag::Insert => a += 1,
            similar::ChangeTag::Delete => r += 1,
            _ => {}
        }
    }
    (a, r)
}

fn is_binary(bytes: &[u8]) -> bool {
    bytes.iter().take(8000).any(|b| *b == 0)
}

pub fn read_file(ws: &Workspace, path: &str, offset: usize, limit: usize) -> Result<ToolOut, String> {
    let r = ws.resolve(path)?;
    let meta = fs::metadata(&r.path).map_err(|e| format!("cannot read {path}: {e}"))?;
    if meta.is_dir() {
        return Err(format!("{path} is a directory; use list_dir"));
    }
    if meta.len() > MAX_READ_BYTES {
        return Err(format!("{path} is {} bytes; too large to read whole. Use search to locate the part you need.", meta.len()));
    }
    let bytes = fs::read(&r.path).map_err(|e| format!("cannot read {path}: {e}"))?;
    if is_binary(&bytes) {
        return Err(format!("{path} looks like a binary file ({} bytes)", bytes.len()));
    }
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = text.lines().collect();
    let start = offset.max(1) - 1;
    let end = (start + limit.max(1)).min(lines.len());
    let mut body = lines.get(start..end).unwrap_or(&[]).join("\n");
    if body.len() > MAX_OUT_CHARS {
        let mut cut = MAX_OUT_CHARS;
        while !body.is_char_boundary(cut) {
            cut -= 1;
        }
        body.truncate(cut);
        body.push_str("\n…[truncated]");
    }
    let header = format!("[{} · lines {}-{} of {}]", ws.rel(&r.path), if lines.is_empty() { 0 } else { start + 1 }, end, lines.len());
    Ok(ToolOut::with(format!("{header}\n{body}"), json!({"path": ws.rel(&r.path), "lines": lines.len()})))
}

pub fn preview_write(ws: &Workspace, path: &str, content: &str) -> Result<Value, String> {
    let r = ws.resolve(path)?;
    let old = fs::read_to_string(&r.path).unwrap_or_default();
    let existed = r.path.exists();
    Ok(json!({"kind": "diff", "path": ws.rel(&r.path), "created": !existed, "diff": unified_diff(&ws.rel(&r.path), &old, content)}))
}

pub fn write_file(ws: &Workspace, path: &str, content: &str) -> Result<ToolOut, String> {
    let r = ws.resolve(path)?;
    if r.path.is_dir() {
        return Err(format!("{path} is a directory"));
    }
    let existed = r.path.exists();
    let old = if existed { fs::read_to_string(&r.path).unwrap_or_default() } else { String::new() };
    if let Some(parent) = r.path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("cannot create folder for {path}: {e}"))?;
    }
    fs::write(&r.path, content).map_err(|e| format!("cannot write {path}: {e}"))?;
    let rel = ws.rel(&r.path);
    let (add, rem) = diff_stats(&old, content);
    Ok(ToolOut::with(
        format!("{} {rel} ({} bytes, +{add} -{rem})", if existed { "Overwrote" } else { "Created" }, content.len()),
        json!({"kind": "diff", "path": rel, "created": !existed, "added": add, "removed": rem, "diff": unified_diff(&rel, &old, content)}),
    ))
}

/// Helps the model correct a failed exact match: say whether only whitespace differs, and show the
/// closest line of the file to the first line it tried to replace.
fn not_found_message(old: &str, find: &str, path: &str) -> String {
    let mut msg = format!("old_string was not found in {path}.");
    let norm = |s: &str| s.lines().map(|l| l.split_whitespace().collect::<Vec<_>>().join(" ")).collect::<Vec<_>>().join("\n");
    if !norm(find).trim().is_empty() && norm(old).contains(norm(find).trim()) {
        msg.push_str(" The text exists but differs in whitespace or indentation; copy it exactly from read_file output.");
    }
    let first = find.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    if !first.is_empty() {
        let best = old
            .lines()
            .enumerate()
            .map(|(i, l)| (i + 1, l, TextDiff::from_chars(l.trim(), first).ratio()))
            .max_by(|a, b| a.2.partial_cmp(&b.2).unwrap_or(std::cmp::Ordering::Equal));
        if let Some((n, line, ratio)) = best {
            if ratio > 0.5 {
                msg.push_str(&format!(" Closest line in the file is {n}: `{}`.", line.trim()));
            }
        }
    }
    msg.push_str(" Re-read the file and copy the exact text, or use write_file to replace the whole file.");
    msg
}

fn apply_edit(old: &str, find: &str, repl: &str, all: bool, path: &str) -> Result<String, String> {
    if find.is_empty() {
        return Err("old_string must not be empty (use write_file to create a file)".into());
    }
    if find == repl {
        return Err("old_string and new_string are identical".into());
    }
    let n = old.matches(find).count();
    if n == 0 {
        return Err(not_found_message(old, find, path));
    }
    if n > 1 && !all {
        return Err(format!("old_string matches {n} places in {path}. Include more surrounding lines to make it unique, or set replace_all."));
    }
    Ok(if all { old.replace(find, repl) } else { old.replacen(find, repl, 1) })
}

pub fn preview_edit(ws: &Workspace, path: &str, find: &str, repl: &str, all: bool) -> Result<Value, String> {
    let r = ws.resolve(path)?;
    let old = fs::read_to_string(&r.path).map_err(|e| format!("cannot read {path}: {e}"))?;
    let new = apply_edit(&old, find, repl, all, path)?;
    Ok(json!({"kind": "diff", "path": ws.rel(&r.path), "created": false, "diff": unified_diff(&ws.rel(&r.path), &old, &new)}))
}

pub fn edit_file(ws: &Workspace, path: &str, find: &str, repl: &str, all: bool) -> Result<ToolOut, String> {
    let r = ws.resolve(path)?;
    let old = fs::read_to_string(&r.path).map_err(|e| format!("cannot read {path}: {e}"))?;
    let new = apply_edit(&old, find, repl, all, path)?;
    fs::write(&r.path, &new).map_err(|e| format!("cannot write {path}: {e}"))?;
    let rel = ws.rel(&r.path);
    let (add, rem) = diff_stats(&old, &new);
    Ok(ToolOut::with(format!("Edited {rel} (+{add} -{rem})"), json!({"kind": "diff", "path": rel, "created": false, "added": add, "removed": rem, "diff": unified_diff(&rel, &old, &new)})))
}

pub fn delete_path(ws: &Workspace, path: &str, recursive: bool) -> Result<ToolOut, String> {
    let r = ws.resolve(path)?;
    if r.path == ws.root {
        return Err("refusing to delete the project root".into());
    }
    let meta = fs::symlink_metadata(&r.path).map_err(|e| format!("cannot delete {path}: {e}"))?;
    let rel = ws.rel(&r.path);
    if meta.is_dir() && !meta.file_type().is_symlink() {
        if recursive {
            fs::remove_dir_all(&r.path)
        } else {
            fs::remove_dir(&r.path)
        }
        .map_err(|e| format!("cannot delete folder {path}: {e} (set recursive to delete a non-empty folder)"))?;
    } else {
        fs::remove_file(&r.path).map_err(|e| format!("cannot delete {path}: {e}"))?;
    }
    Ok(ToolOut::with(format!("Deleted {rel}"), json!({"kind": "delete", "path": rel})))
}

pub fn move_path(ws: &Workspace, from: &str, to: &str, overwrite: bool) -> Result<ToolOut, String> {
    let a = ws.resolve(from)?;
    let b = ws.resolve(to)?;
    if a.path == ws.root {
        return Err("cannot move the project root".into());
    }
    if !a.path.exists() {
        return Err(format!("{from} does not exist"));
    }
    if b.path.exists() && !overwrite {
        return Err(format!("{to} already exists; set overwrite to replace it"));
    }
    if b.path.starts_with(&a.path) {
        return Err("cannot move a folder into itself".into());
    }
    if let Some(p) = b.path.parent() {
        fs::create_dir_all(p).map_err(|e| format!("cannot create {}: {e}", p.display()))?;
    }
    fs::rename(&a.path, &b.path).map_err(|e| format!("cannot move: {e}"))?;
    Ok(ToolOut::with(format!("Moved {} → {}", ws.rel(&a.path), ws.rel(&b.path)), json!({"kind": "move", "from": ws.rel(&a.path), "to": ws.rel(&b.path)})))
}

pub fn list_dir(ws: &Workspace, path: &str, depth: usize) -> Result<ToolOut, String> {
    let r = ws.resolve(path)?;
    if !r.path.is_dir() {
        return Err(format!("{path} is not a directory"));
    }
    let mut lines = vec![];
    fn walk(dir: &Path, root: &Path, depth: usize, max: usize, lines: &mut Vec<String>) {
        let Ok(rd) = fs::read_dir(dir) else { return };
        let mut entries: Vec<_> = rd.flatten().collect();
        entries.sort_by_key(|e| (!e.path().is_dir(), e.file_name()));
        for e in entries {
            if lines.len() >= 500 {
                return;
            }
            let p = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            let rel = p.strip_prefix(root).unwrap_or(&p).display().to_string();
            if p.is_dir() {
                lines.push(format!("{rel}/"));
                if depth < max && !SKIP_DIRS.contains(&name.as_str()) {
                    walk(&p, root, depth + 1, max, lines);
                }
            } else {
                let size = e.metadata().map(|m| m.len()).unwrap_or(0);
                lines.push(format!("{rel}  ({size} B)"));
            }
        }
    }
    walk(&r.path, &r.path, 1, depth.clamp(1, 4), &mut lines);
    let more = if lines.len() >= 500 { "\n…[listing truncated at 500 entries]" } else { "" };
    let head = ws.rel(&r.path);
    let head = if head.is_empty() { ".".to_string() } else { head };
    Ok(ToolOut::text(format!("{head}:\n{}{more}", if lines.is_empty() { "(empty)".into() } else { lines.join("\n") })))
}

pub fn search(ws: &Workspace, pattern: &str, path: &str, glob: Option<&str>, ci: bool, max: usize) -> Result<ToolOut, String> {
    let r = ws.resolve(if path.is_empty() { "." } else { path })?;
    let re = regex::RegexBuilder::new(pattern).case_insensitive(ci).build().map_err(|e| format!("invalid regex: {e}"))?;
    let mut wb = ignore::WalkBuilder::new(&r.path);
    wb.hidden(false).filter_entry(|e| !SKIP_DIRS.contains(&e.file_name().to_string_lossy().as_ref()));
    if let Some(g) = glob {
        let mut ob = ignore::overrides::OverrideBuilder::new(&r.path);
        ob.add(g).map_err(|e| format!("invalid glob: {e}"))?;
        wb.overrides(ob.build().map_err(|e| format!("invalid glob: {e}"))?);
    }
    let (mut hits, mut files, mut skipped_sensitive) = (vec![], 0usize, 0usize);
    for ent in wb.build().flatten() {
        let p = ent.path();
        if !p.is_file() {
            continue;
        }
        if is_sensitive(p) {
            skipped_sensitive += 1;
            continue;
        }
        if fs::metadata(p).map(|m| m.len() > 1_000_000).unwrap_or(true) {
            continue;
        }
        let Ok(bytes) = fs::read(p) else { continue };
        if is_binary(&bytes) {
            continue;
        }
        files += 1;
        for (i, line) in String::from_utf8_lossy(&bytes).lines().enumerate() {
            if re.is_match(line) {
                let l: String = line.chars().take(300).collect();
                hits.push(format!("{}:{}: {}", ws.rel(p), i + 1, l.trim_end()));
                if hits.len() >= max {
                    break;
                }
            }
        }
        if hits.len() >= max {
            break;
        }
    }
    let note = if skipped_sensitive > 0 { format!("\n({skipped_sensitive} credential-like file(s) were skipped)") } else { String::new() };
    if hits.is_empty() {
        return Ok(ToolOut::text(format!("No matches in {files} files.{note}")));
    }
    let cap = if hits.len() >= max { format!("\n…[stopped at {max} matches]") } else { String::new() };
    Ok(ToolOut::text(format!("{}{cap}{note}", hits.join("\n"))))
}

pub fn find_files(ws: &Workspace, glob: &str, path: &str, max: usize) -> Result<ToolOut, String> {
    let r = ws.resolve(if path.is_empty() { "." } else { path })?;
    let mut ob = ignore::overrides::OverrideBuilder::new(&r.path);
    ob.add(glob).map_err(|e| format!("invalid glob: {e}"))?;
    let mut wb = ignore::WalkBuilder::new(&r.path);
    wb.hidden(false).overrides(ob.build().map_err(|e| format!("invalid glob: {e}"))?).filter_entry(|e| !SKIP_DIRS.contains(&e.file_name().to_string_lossy().as_ref()));
    let mut out = vec![];
    for ent in wb.build().flatten() {
        if ent.path().is_file() {
            out.push(ws.rel(ent.path()));
            if out.len() >= max {
                break;
            }
        }
    }
    out.sort();
    Ok(ToolOut::text(if out.is_empty() { "No files match.".to_string() } else { out.join("\n") }))
}

/// File tree for the UI (directories first, ignoring heavy build folders).
pub fn tree(root: &Path, depth: usize) -> Value {
    fn node(p: &Path, depth: usize, max: usize) -> Value {
        let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        if p.is_dir() {
            let mut kids = vec![];
            if depth < max && !SKIP_DIRS.contains(&name.as_str()) {
                if let Ok(rd) = fs::read_dir(p) {
                    let mut es: Vec<_> = rd.flatten().map(|e| e.path()).collect();
                    es.sort_by_key(|p| (!p.is_dir(), p.file_name().map(|n| n.to_ascii_lowercase())));
                    for e in es.into_iter().take(300) {
                        kids.push(node(&e, depth + 1, max));
                    }
                }
            }
            json!({"name": name, "dir": true, "children": kids})
        } else {
            json!({"name": name, "dir": false})
        }
    }
    node(root, 0, depth)
}

/// Size and modification time of every (non-ignored) file, so a task's real effect on disk can be
/// compared before/after regardless of which tool (or shell command) made the change.
pub fn snapshot(root: &Path, max: usize) -> Value {
    let mut wb = ignore::WalkBuilder::new(root);
    wb.hidden(false).filter_entry(|e| !SKIP_DIRS.contains(&e.file_name().to_string_lossy().as_ref()));
    let mut files = serde_json::Map::new();
    for ent in wb.build().flatten() {
        if !ent.path().is_file() {
            continue;
        }
        if files.len() >= max {
            return json!({"files": files, "truncated": true});
        }
        if let Ok(m) = ent.metadata() {
            let mt = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_nanos() as u64).unwrap_or(0);
            let rel = ent.path().strip_prefix(root).unwrap_or(ent.path()).display().to_string();
            files.insert(rel, json!([m.len(), mt]));
        }
    }
    json!({"files": files, "truncated": false})
}
