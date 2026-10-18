//! Platform adapter: the only place that knows how to spawn a shell, kill a process
//! tree or scrub the environment. Linux is the validated target; the macOS path shares
//! the unix implementation and the Windows path is an untested adapter.

use tokio::process::Command;

/// Environment variables whose *names* suggest a secret are never passed to commands.
pub fn is_secret_env(name: &str) -> bool {
    let n = name.to_ascii_uppercase();
    ["TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY", "APIKEY", "PRIVATE_KEY", "ACCESS_KEY",
     "SESSION_KEY", "AUTH", "COOKIE"]
        .iter()
        .any(|k| n.contains(k))
        || n.starts_with("SARNESS_")
        || n == "SSH_AUTH_SOCK"
        || n == "GPG_AGENT_INFO"
        || n.ends_with("_PAT")
}

/// Builds a shell command running `script` with cwd, a scrubbed environment and its own
/// process group so the whole tree can be terminated.
pub fn shell_command(script: &str, cwd: &std::path::Path) -> Command {
    #[cfg(unix)]
    let mut c = {
        let shell = if std::path::Path::new("/bin/bash").exists() { "/bin/bash" } else { "/bin/sh" };
        let mut c = Command::new(shell);
        c.arg("-c").arg(script);
        c.process_group(0);
        c
    };
    #[cfg(windows)]
    let mut c = {
        let mut c = Command::new("cmd");
        c.arg("/C").arg(script);
        c
    };
    apply_env(&mut c);
    c.current_dir(cwd);
    c.stdin(std::process::Stdio::null());
    c.stdout(std::process::Stdio::piped());
    c.stderr(std::process::Stdio::piped());
    c.kill_on_drop(true);
    c
}

/// A program invoked directly with fixed arguments (no shell): used for git.
pub fn program_command(program: &str, args: &[&str], cwd: &std::path::Path) -> Command {
    let mut c = Command::new(program);
    c.args(args);
    #[cfg(unix)]
    c.process_group(0);
    apply_env(&mut c);
    c.current_dir(cwd);
    c.stdin(std::process::Stdio::null());
    c.stdout(std::process::Stdio::piped());
    c.stderr(std::process::Stdio::piped());
    c.kill_on_drop(true);
    c
}

fn apply_env(c: &mut Command) {
    c.env_clear();
    for (k, v) in std::env::vars() {
        if !is_secret_env(&k) {
            c.env(k, v);
        }
    }
    // Non-interactive, plain-text tooling so output is readable by the model.
    c.env("CI", "1")
        .env("TERM", "dumb")
        .env("NO_COLOR", "1")
        .env("PAGER", "cat")
        .env("GIT_PAGER", "cat")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("DEBIAN_FRONTEND", "noninteractive")
        .env("npm_config_yes", "true");
}

/// Terminates a process and all of its descendants (graceful first, then forced).
pub async fn kill_tree(pid: u32) {
    #[cfg(unix)]
    {
        let pg = pid as i32;
        unsafe {
            libc::kill(-pg, libc::SIGTERM);
        }
        for _ in 0..10 {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            if unsafe { libc::kill(-pg, 0) } != 0 {
                return; // group gone
            }
        }
        unsafe {
            libc::kill(-pg, libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill").args(["/T", "/F", "/PID", &pid.to_string()]).status().await;
    }
}

#[cfg(unix)]
pub fn group_alive(pid: u32) -> bool {
    unsafe { libc::kill(-(pid as i32), 0) == 0 }
}

pub fn os_name() -> &'static str {
    std::env::consts::OS
}
