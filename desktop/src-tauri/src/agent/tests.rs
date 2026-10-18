//! End-to-end tests of the native tool runtime against real temporary directories.

use super::policy::Mode;
use super::{Engine, ToolRequest, ToolResponse};
use serde_json::{json, Value};

struct Env {
    _d: tempfile::TempDir,
    root: String,
    engine: Engine,
}

fn env() -> Env {
    let d = tempfile::tempdir().unwrap();
    let engine = Engine::new();
    let root = engine.register_workspace(d.path().to_str().unwrap()).unwrap().display().to_string();
    Env { _d: d, root, engine }
}

impl Env {
    async fn call_as(&self, run: &str, tool: &str, args: Value, mode: Mode, approval: Option<String>) -> ToolResponse {
        self.engine
            .call(ToolRequest { workspace: self.root.clone(), run_id: run.into(), call_id: "c".into(), tool: tool.into(), args, mode, approval_id: approval }, None)
            .await
    }
    async fn auto(&self, tool: &str, args: Value) -> ToolResponse {
        self.call_as("r1", tool, args, Mode::WorkspaceAuto, None).await
    }
    fn path(&self, rel: &str) -> std::path::PathBuf {
        std::path::Path::new(&self.root).join(rel)
    }
}

/// True once the process is gone (or only a zombie awaiting reaping).
async fn gone(pid: &str) -> bool {
    for _ in 0..40 {
        let st = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
        if st.is_empty() || st.lines().any(|l| l.starts_with("State:") && l.contains('Z')) {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    false
}

fn pid_after(text: &str, marker: &str) -> String {
    text.lines().find_map(|l| l.strip_prefix(marker)).unwrap_or_else(|| panic!("no {marker} in {text}")).trim().to_string()
}

fn ok(r: ToolResponse) -> (String, Value) {
    match r {
        ToolResponse::Ok { content, ui } => (content, ui),
        other => panic!("expected Ok, got {other:?}"),
    }
}

#[tokio::test]
async fn creates_edits_and_reports_real_diffs() {
    let e = env();
    let (c, ui) = ok(e.auto("write_file", json!({"path": "src/app.js", "content": "const a = 1;\nconsole.log(a);\n"})).await);
    assert!(c.starts_with("Created src/app.js"));
    assert_eq!(std::fs::read_to_string(e.path("src/app.js")).unwrap(), "const a = 1;\nconsole.log(a);\n");
    assert_eq!(ui["created"], true);
    let (c, ui) = ok(e.auto("edit_file", json!({"path": "src/app.js", "old_string": "a = 1", "new_string": "a = 2"})).await);
    assert!(c.contains("+1 -1"));
    assert!(ui["diff"].as_str().unwrap().contains("-const a = 1;") && ui["diff"].as_str().unwrap().contains("+const a = 2;"));
    // ambiguous and missing matches are errors the model can act on
    e.auto("write_file", json!({"path": "d.txt", "content": "x x"})).await;
    match e.auto("edit_file", json!({"path": "d.txt", "old_string": "x", "new_string": "y"})).await {
        ToolResponse::Error { content, .. } => assert!(content.contains("matches 2 places")),
        o => panic!("{o:?}"),
    }
    ok(e.auto("write_file", json!({"path": "code.js", "content": "function slugify(text) {\n    return text.trim();\n}\n"})).await);
    match e.auto("edit_file", json!({"path": "code.js", "old_string": "\treturn text.trim();", "new_string": "x"})).await {
        ToolResponse::Error { content, .. } => assert!(content.contains("whitespace or indentation") && content.contains("Closest line in the file is 2"), "{content}"),
        o => panic!("{o:?}"),
    }
    match e.auto("edit_file", json!({"path": "code.js", "old_string": "return text.toUpper();", "new_string": "x"})).await {
        ToolResponse::Error { content, .. } => assert!(content.contains("Closest line in the file is 2") && content.contains("return text.trim();"), "{content}"),
        o => panic!("{o:?}"),
    }
    match e.auto("edit_file", json!({"path": "d.txt", "old_string": "zzz", "new_string": "y"})).await {
        ToolResponse::Error { content, .. } => assert!(content.contains("not found")),
        o => panic!("{o:?}"),
    }
    let (c, _) = ok(e.auto("read_file", json!({"path": "src/app.js"})).await);
    assert!(c.contains("const a = 2;"));
    let (c, _) = ok(e.auto("list_dir", json!({"depth": 2})).await);
    assert!(c.contains("src/") && c.contains("src/app.js"));
    let (c, _) = ok(e.auto("search", json!({"pattern": "console\\.log"})).await);
    assert!(c.contains("src/app.js:2:"));
    ok(e.auto("move_path", json!({"from": "d.txt", "to": "sub/e.txt"})).await);
    assert!(e.path("sub/e.txt").exists() && !e.path("d.txt").exists());
    ok(e.auto("delete_path", json!({"path": "sub/e.txt"})).await);
    assert!(!e.path("sub/e.txt").exists());
}

#[tokio::test]
async fn workspace_boundary_is_enforced() {
    let e = env();
    let outside = tempfile::tempdir().unwrap();
    let target = outside.path().join("victim.txt");
    std::fs::write(&target, "keep").unwrap();
    for (tool, args) in [
        ("write_file", json!({"path": target.to_str().unwrap(), "content": "pwned"})),
        ("write_file", json!({"path": "../escape.txt", "content": "pwned"})),
        ("delete_path", json!({"path": target.to_str().unwrap()})),
        ("read_file", json!({"path": target.to_str().unwrap()})),
        ("edit_file", json!({"path": target.to_str().unwrap(), "old_string": "keep", "new_string": "x"})),
        ("list_dir", json!({"path": "/etc"})),
    ] {
        for mode in [Mode::Ask, Mode::WorkspaceAuto] {
            match e.call_as("r", tool, args.clone(), mode, None).await {
                ToolResponse::NeedsApproval { .. } => {}
                o => panic!("{tool} {args} {mode:?}: expected approval request, got {o:?}"),
            }
        }
    }
    assert_eq!(std::fs::read_to_string(&target).unwrap(), "keep");
    assert!(!std::path::Path::new(&e.root).parent().unwrap().join("escape.txt").exists());
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(outside.path(), e.path("link")).unwrap();
        match e.auto("write_file", json!({"path": "link/victim.txt", "content": "pwned"})).await {
            ToolResponse::NeedsApproval { .. } => {}
            o => panic!("symlink escape not caught: {o:?}"),
        }
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "keep");
    }
    // the project root itself can never be deleted
    assert!(matches!(e.call_as("r", "delete_path", json!({"path": ".", "recursive": true}), Mode::FullAccess, None).await, ToolResponse::Denied { .. }));
}

#[tokio::test]
async fn unregistered_workspaces_are_refused() {
    let e = env();
    let other = tempfile::tempdir().unwrap();
    let r = e.engine.call(ToolRequest { workspace: other.path().display().to_string(), run_id: "r".into(), call_id: "c".into(), tool: "list_dir".into(), args: json!({}), mode: Mode::FullAccess, approval_id: None }, None).await;
    assert!(matches!(r, ToolResponse::Denied { .. }), "{r:?}");
}

#[tokio::test]
async fn approvals_are_bound_to_the_exact_call_and_single_use() {
    let e = env();
    let args = json!({"path": "a.txt", "content": "hello"});
    let id = match e.call_as("r", "write_file", args.clone(), Mode::Ask, None).await {
        ToolResponse::NeedsApproval { approval_id, preview, .. } => {
            assert!(preview["diff"].as_str().unwrap().contains("+hello"));
            assert!(!e.path("a.txt").exists(), "nothing may be written before approval");
            approval_id
        }
        o => panic!("{o:?}"),
    };
    // a different payload with the same approval id is not approved
    assert!(matches!(e.call_as("r", "write_file", json!({"path": "a.txt", "content": "EVIL"}), Mode::Ask, Some(id.clone())).await, ToolResponse::NeedsApproval { .. }));
    assert!(!e.path("a.txt").exists());
    ok(e.call_as("r", "write_file", args.clone(), Mode::Ask, Some(id.clone())).await);
    assert_eq!(std::fs::read_to_string(e.path("a.txt")).unwrap(), "hello");
    // single use
    assert!(matches!(e.call_as("r", "write_file", args, Mode::Ask, Some(id)).await, ToolResponse::NeedsApproval { .. }));
}

#[tokio::test]
async fn commands_return_real_stdout_stderr_and_exit_codes() {
    let e = env();
    let (c, ui) = ok(e.auto("run_command", json!({"command": "echo out; echo err 1>&2; exit 3"})).await);
    assert_eq!(ui["exit_code"], 3);
    assert!(c.contains("exit code: 3") && c.contains("--- stdout ---\nout") && c.contains("--- stderr ---\nerr"));
    // runs in the workspace, locally
    let (_, ui) = ok(e.auto("run_command", json!({"command": "pwd && touch made-by-command.txt"})).await);
    assert!(ui["stdout"].as_str().unwrap().trim_end().ends_with(&e.root));
    assert!(e.path("made-by-command.txt").exists());
    // real tools are really used
    let (_, ui) = ok(e.auto("run_command", json!({"command": "python3 -c 'print(6*7)'"})).await);
    assert_eq!(ui["stdout"].as_str().unwrap().trim(), "42");
    // output limits keep head and tail
    let (_, ui) = ok(e.auto("run_command", json!({"command": "seq 1 200000"})).await);
    let out = ui["stdout"].as_str().unwrap();
    assert!(out.starts_with("1\n") && out.trim_end().ends_with("200000") && out.contains("bytes omitted") && ui["truncated"] == true);
    assert!(out.len() < 40_000);
}

#[tokio::test]
async fn timeouts_kill_the_whole_process_tree() {
    let e = env();
    let t0 = std::time::Instant::now();
    let (c, ui) = ok(e.auto("run_command", json!({"command": "sleep 31337 & echo bgpid=$!; echo fgpid=$$; sleep 31337; echo never", "timeout_s": 1})).await);
    assert_eq!(ui["timed_out"], true, "{c}");
    assert!(t0.elapsed().as_secs() < 10 && c.contains("timed out"));
    let out = ui["stdout"].as_str().unwrap();
    assert!(gone(&pid_after(out, "bgpid=")).await, "background child survived the timeout");
    assert!(gone(&pid_after(out, "fgpid=")).await);
}

#[tokio::test]
async fn cancel_stops_a_running_command_and_blocks_further_calls() {
    let e = std::sync::Arc::new(env());
    let e2 = e.clone();
    let task = tokio::spawn(async move { e2.call_as("run-x", "run_command", json!({"command": "sleep 31338 & echo bgpid=$!; wait; echo done", "timeout_s": 120}), Mode::WorkspaceAuto, None).await });
    tokio::time::sleep(std::time::Duration::from_millis(900)).await;
    let t0 = std::time::Instant::now();
    e.engine.cancel("run-x").await;
    let (c, ui) = ok(task.await.unwrap());
    assert_eq!(ui["cancelled"], true, "{c}");
    assert!(t0.elapsed().as_secs() < 5);
    assert!(gone(&pid_after(ui["stdout"].as_str().unwrap(), "bgpid=")).await, "child survived cancel");
    assert!(!ui["stdout"].as_str().unwrap().contains("done"));
    assert!(matches!(e.call_as("run-x", "list_dir", json!({}), Mode::WorkspaceAuto, None).await, ToolResponse::Denied { .. }));
    e.engine.reset_run("run-x");
    ok(e.call_as("run-x", "list_dir", json!({}), Mode::WorkspaceAuto, None).await);
}

#[tokio::test]
async fn background_processes_are_readable_and_stopped_at_end_of_run() {
    let e = env();
    let (c, ui) = ok(e.call_as("run-bg", "start_process", json!({"command": "echo listening; echo pid=$$; sleep 31339", "wait_ms": 1500}), Mode::WorkspaceAuto, None).await);
    assert!(c.contains("listening"));
    let pid = pid_after(&c, "pid=");
    let id = ui["id"].as_str().unwrap().to_string();
    let (c, _) = ok(e.call_as("run-bg", "process_output", json!({"id": id}), Mode::WorkspaceAuto, None).await);
    assert!(c.contains("running") && c.contains("listening"));
    assert_eq!(e.engine.end_run("run-bg").await, 1);
    assert!(gone(&pid).await, "background process survived end_run");
}

#[tokio::test]
async fn secrets_are_not_visible_to_commands_and_are_redacted_from_output() {
    let e = env();
    std::env::set_var("MY_SERVICE_API_TOKEN", "super-secret-value-123");
    let (_, ui) = ok(e.auto("run_command", json!({"command": "env"})).await);
    assert!(!ui["stdout"].as_str().unwrap().contains("super-secret-value-123"));
    let (_, ui) = ok(e.auto("run_command", json!({"command": "echo key=sk-abcdefghijklmnopqrstuvwxyz0123456789"})).await);
    assert!(ui["stdout"].as_str().unwrap().contains("[REDACTED]") && !ui["stdout"].as_str().unwrap().contains("abcdefghijklmnop"));
    // credential-like files need approval even to be read, and bulk search skips them
    std::fs::write(e.path(".env"), "DB_PASSWORD=hunter2\n").unwrap();
    assert!(matches!(e.auto("read_file", json!({"path": ".env"})).await, ToolResponse::NeedsApproval { .. }));
    std::fs::write(e.path("code.txt"), "DB_PASSWORD placeholder\n").unwrap();
    let (c, _) = ok(e.auto("search", json!({"pattern": "DB_PASSWORD"})).await);
    assert!(c.contains("code.txt") && !c.contains("hunter2") && c.contains("skipped"));
}

#[tokio::test]
async fn risky_commands_need_approval_and_refused_ones_never_run() {
    let e = env();
    for c in ["npm install left-pad", "cat /etc/hostname", "sudo true", "git push origin main"] {
        assert!(matches!(e.auto("run_command", json!({"command": c})).await, ToolResponse::NeedsApproval { .. }), "{c}");
    }
    std::fs::write(e.path("keep.txt"), "x").unwrap();
    assert!(matches!(e.call_as("r", "run_command", json!({"command": "rm -rf /"}), Mode::FullAccess, None).await, ToolResponse::Denied { .. }));
    assert!(e.path("keep.txt").exists());
    // Ask mode: even harmless commands wait for the user
    assert!(matches!(e.call_as("r", "run_command", json!({"command": "ls"}), Mode::Ask, None).await, ToolResponse::NeedsApproval { .. }));
}

#[tokio::test]
async fn invalid_tool_calls_become_errors_for_the_model() {
    let e = env();
    for (tool, args, needle) in [
        ("nope", json!({}), "Unknown tool"),
        ("read_file", json!({}), "missing required argument `path`"),
        ("read_file", json!({"path": 5}), "wrong type"),
        ("run_command", json!({"command": "ls", "shell": "zsh"}), "unknown argument"),
    ] {
        match e.auto(tool, args).await {
            ToolResponse::Error { content, .. } => assert!(content.contains(needle), "{content}"),
            o => panic!("{o:?}"),
        }
    }
}

#[tokio::test]
async fn git_status_diff_branch_and_commit() {
    let e = env();
    ok(e.auto("run_command", json!({"command": "git init -q -b main && git config user.email t@example.com && git config user.name Tester"})).await);
    ok(e.auto("write_file", json!({"path": "a.txt", "content": "one\n"})).await);
    let (c, _) = ok(e.auto("git_status", json!({})).await);
    assert!(c.contains("## No commits yet on main") && c.contains("?? a.txt"));
    // committing needs approval in Auto mode, and runs once granted
    let id = match e.auto("git_commit", json!({"message": "first"})).await {
        ToolResponse::NeedsApproval { approval_id, .. } => approval_id,
        o => panic!("{o:?}"),
    };
    let (c, ui) = ok(e.call_as("r1", "git_commit", json!({"message": "first"}), Mode::WorkspaceAuto, Some(id)).await);
    assert!(c.contains("first") && ui["files"][0] == "a.txt");
    ok(e.auto("edit_file", json!({"path": "a.txt", "old_string": "one", "new_string": "two"})).await);
    let (c, _) = ok(e.auto("git_diff", json!({})).await);
    assert!(c.contains("-one") && c.contains("+two"));
    ok(e.auto("git_branch", json!({"action": "create", "name": "feature/x"})).await);
    let (c, _) = ok(e.auto("git_branch", json!({"action": "list"})).await);
    assert!(c.contains("* feature/x"));
    assert!(matches!(e.auto("git_branch", json!({"action": "create", "name": "-bad"})).await, ToolResponse::Error { .. }));
    let (c, _) = ok(e.auto("git_log", json!({})).await);
    assert!(c.contains("first"));
    // credential-like files are never committed
    std::fs::write(e.path(".env"), "TOKEN=1\n").unwrap();
    match e.call_as("r1", "git_commit", json!({"message": "oops"}), Mode::FullAccess, None).await {
        ToolResponse::Error { content, .. } => assert!(content.contains("credential-like") && content.contains(".env")),
        o => panic!("{o:?}"),
    }
    let (c, _) = ok(e.auto("git_status", json!({})).await);
    assert!(c.contains("?? .env"), "unstaged again: {c}");
}

#[tokio::test]
async fn diagnostics_identify_the_local_machine() {
    let e = env();
    let (c, _) = ok(e.auto("diagnostics", json!({})).await);
    assert!(c.contains("execution host:") && c.contains(&e.root) && c.contains("never runs tools") && c.contains("git:"));
}

#[tokio::test]
async fn a_small_project_can_be_built_tested_broken_and_fixed_with_the_tools() {
    // The scripted equivalent of the agent's tool calls for the end-to-end scenario.
    let e = env();
    ok(e.auto("write_file", json!({"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"})).await);
    ok(e.auto("write_file", json!({"path": "test_calc.py", "content": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"})).await);
    let (_, ui) = ok(e.auto("run_command", json!({"command": "python3 -B -c 'import test_calc; test_calc.test_add()' 2>&1"})).await);
    assert_ne!(ui["exit_code"], 0, "the seeded bug must fail the test");
    ok(e.auto("edit_file", json!({"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})).await);
    let (_, ui) = ok(e.auto("run_command", json!({"command": "python3 -B -c 'import test_calc; test_calc.test_add(); print(\"PASS\")'"})).await);
    assert_eq!(ui["exit_code"], 0);
    assert!(ui["stdout"].as_str().unwrap().contains("PASS"));
}

#[test]
fn snapshot_sees_changes_made_by_any_means() {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(d.path().join("a.txt"), "x").unwrap();
    std::fs::create_dir(d.path().join("node_modules")).unwrap();
    std::fs::write(d.path().join("node_modules/skip.js"), "x").unwrap();
    let before = super::fstools::snapshot(d.path(), 100);
    assert!(before["files"]["a.txt"].is_array() && before["files"].get("node_modules/skip.js").is_none());
    std::fs::write(d.path().join("empty.js"), "").unwrap();
    std::fs::write(d.path().join("a.txt"), "longer").unwrap();
    let after = super::fstools::snapshot(d.path(), 100);
    assert_eq!(after["files"]["empty.js"][0], 0);
    assert_ne!(before["files"]["a.txt"], after["files"]["a.txt"]);
    assert_eq!(super::fstools::snapshot(d.path(), 1)["truncated"], true);
}

#[tokio::test]
async fn shutdown_stops_every_background_process_even_mid_task() {
    let e = env();
    let (c, _) = ok(e.call_as("run-sd", "start_process", json!({"command": "echo pid=$$; sleep 31340", "wait_ms": 1200}), Mode::WorkspaceAuto, None).await);
    let pid = pid_after(&c, "pid=");
    assert!(std::path::Path::new(&format!("/proc/{pid}")).exists(), "process should be running before shutdown");
    e.engine.shutdown().await; // what the app does on exit
    assert!(gone(&pid).await, "background process survived app shutdown");
}
