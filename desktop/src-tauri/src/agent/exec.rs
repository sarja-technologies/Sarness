//! Local command execution: streaming output, timeouts, cancellation, process groups, and
//! long-running background processes (dev servers). Everything here runs on the machine
//! that runs Sarness; nothing is ever sent to the inference server.

use super::platform;
use serde::Serialize;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::watch;

pub const HEAD_BYTES: usize = 6 * 1024;
pub const TAIL_BYTES: usize = 18 * 1024;

#[derive(Serialize, Clone, Debug)]
pub struct OutputEvent {
    pub run_id: String,
    pub call_id: String,
    pub stream: &'static str,
    pub text: String,
}

pub type Emit = Arc<dyn Fn(OutputEvent) + Send + Sync>;

/// Keeps the beginning and end of a potentially huge output.
#[derive(Default)]
pub struct Capped {
    head: Vec<u8>,
    tail: Vec<u8>,
    pub total: usize,
}

impl Capped {
    pub fn push(&mut self, b: &[u8]) {
        self.total += b.len();
        let room = HEAD_BYTES.saturating_sub(self.head.len());
        let (h, t) = b.split_at(room.min(b.len()));
        self.head.extend_from_slice(h);
        self.tail.extend_from_slice(t);
        if self.tail.len() > TAIL_BYTES * 2 {
            let cut = self.tail.len() - TAIL_BYTES;
            self.tail.drain(..cut);
        }
    }
    pub fn truncated(&self) -> bool {
        self.total > self.head.len() + self.tail.len().min(TAIL_BYTES) || self.tail.len() > TAIL_BYTES
    }
    pub fn render(&self) -> String {
        let head = String::from_utf8_lossy(&self.head).into_owned();
        let tail_slice = if self.tail.len() > TAIL_BYTES { &self.tail[self.tail.len() - TAIL_BYTES..] } else { &self.tail[..] };
        let omitted = self.total - self.head.len() - tail_slice.len();
        if omitted == 0 {
            return head + &String::from_utf8_lossy(tail_slice);
        }
        format!("{head}\n…[{omitted} bytes omitted]…\n{}", String::from_utf8_lossy(tail_slice))
    }
}

/// Per-run control: cancellation and the process groups to kill.
pub struct RunCtl {
    pub cancel_tx: watch::Sender<bool>,
    pub pids: Mutex<Vec<u32>>,
}

impl RunCtl {
    pub fn new() -> Arc<Self> {
        Arc::new(Self { cancel_tx: watch::channel(false).0, pids: Mutex::new(vec![]) })
    }
    pub fn cancelled(&self) -> bool {
        *self.cancel_tx.borrow()
    }
}

#[derive(Serialize, Debug, Clone)]
pub struct ExecResult {
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub cancelled: bool,
    pub duration_ms: u128,
    pub truncated: bool,
}

pub fn redact(s: &str) -> String {
    use std::sync::OnceLock;
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_\-]{16,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abp]-[A-Za-z0-9\-]{10,}|(?i)bearer\s+[A-Za-z0-9._\-]{24,}")
            .unwrap()
    });
    re.replace_all(s, "[REDACTED]").into_owned()
}

fn pump<R: AsyncReadExt + Unpin + Send + 'static>(
    mut r: R,
    buf: Arc<Mutex<Capped>>,
    stream: &'static str,
    run_id: String,
    call_id: String,
    emit: Option<Emit>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut chunk = [0u8; 4096];
        loop {
            match r.read(&mut chunk).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    buf.lock().unwrap().push(&chunk[..n]);
                    if let Some(e) = &emit {
                        e(OutputEvent { run_id: run_id.clone(), call_id: call_id.clone(), stream, text: redact(&String::from_utf8_lossy(&chunk[..n])) });
                    }
                }
            }
        }
    })
}

/// Runs a command to completion (or timeout / cancellation).
pub async fn run(mut cmd: Command, timeout: Duration, ctl: &Arc<RunCtl>, run_id: &str, call_id: &str, emit: Option<Emit>) -> Result<ExecResult, String> {
    if ctl.cancelled() {
        return Ok(ExecResult { exit_code: None, stdout: String::new(), stderr: String::new(), timed_out: false, cancelled: true, duration_ms: 0, truncated: false });
    }
    let t0 = Instant::now();
    let mut child = cmd.spawn().map_err(|e| format!("could not start the command: {e}"))?;
    let pid = child.id().unwrap_or(0);
    ctl.pids.lock().unwrap().push(pid);
    let out = Arc::new(Mutex::new(Capped::default()));
    let err = Arc::new(Mutex::new(Capped::default()));
    let h1 = pump(child.stdout.take().unwrap(), out.clone(), "stdout", run_id.into(), call_id.into(), emit.clone());
    let h2 = pump(child.stderr.take().unwrap(), err.clone(), "stderr", run_id.into(), call_id.into(), emit);
    let mut cancel_rx = ctl.cancel_tx.subscribe();
    let (mut timed_out, mut cancelled) = (false, false);
    let status = tokio::select! {
        s = child.wait() => s.ok(),
        _ = tokio::time::sleep(timeout) => { timed_out = true; None }
        _ = async { while !*cancel_rx.borrow() { if cancel_rx.changed().await.is_err() { std::future::pending::<()>().await } } } => { cancelled = true; None }
    };
    if timed_out || cancelled {
        platform::kill_tree(pid).await;
        let _ = child.wait().await;
    } else {
        // Leftover background children may keep the pipes open; do not wait for them forever.
        #[cfg(unix)]
        if platform::group_alive(pid) {
            platform::kill_tree(pid).await;
        }
    }
    let _ = tokio::time::timeout(Duration::from_millis(800), async {
        let _ = h1.await;
        let _ = h2.await;
    })
    .await;
    ctl.pids.lock().unwrap().retain(|p| *p != pid);
    let (o, e) = (out.lock().unwrap(), err.lock().unwrap());
    Ok(ExecResult {
        exit_code: status.and_then(|s| s.code()),
        stdout: redact(&o.render()),
        stderr: redact(&e.render()),
        timed_out,
        cancelled,
        duration_ms: t0.elapsed().as_millis(),
        truncated: o.truncated() || e.truncated(),
    })
}

// ---------------------------------------------------------------- background
pub struct Proc {
    pub id: String,
    pub run_id: String,
    pub command: String,
    pub pid: u32,
    pub started: Instant,
    pub buf: Arc<Mutex<Capped>>,
    pub exit: Arc<Mutex<Option<Option<i32>>>>,
}

#[derive(Default)]
pub struct Processes {
    pub map: Mutex<HashMap<String, Arc<Proc>>>,
}

impl Processes {
    pub fn start(&self, mut cmd: Command, run_id: &str, command: &str) -> Result<Arc<Proc>, String> {
        let mut child = cmd.spawn().map_err(|e| format!("could not start the process: {e}"))?;
        let pid = child.id().unwrap_or(0);
        let buf = Arc::new(Mutex::new(Capped::default()));
        let exit = Arc::new(Mutex::new(None));
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(1);
        let id = format!("proc-{}", NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed));
        drop(pump(child.stdout.take().unwrap(), buf.clone(), "stdout", run_id.into(), id.clone(), None));
        drop(pump(child.stderr.take().unwrap(), buf.clone(), "stderr", run_id.into(), id.clone(), None));
        let ex = exit.clone();
        tokio::spawn(async move {
            let st = child.wait().await.ok().and_then(|s| s.code());
            *ex.lock().unwrap() = Some(st);
        });
        let p = Arc::new(Proc { id: id.clone(), run_id: run_id.into(), command: command.into(), pid, started: Instant::now(), buf, exit });
        self.map.lock().unwrap().insert(id, p.clone());
        Ok(p)
    }
    pub fn get(&self, id: &str) -> Option<Arc<Proc>> {
        self.map.lock().unwrap().get(id).cloned()
    }
    pub fn list(&self) -> Vec<Arc<Proc>> {
        self.map.lock().unwrap().values().cloned().collect()
    }
    pub async fn stop(&self, id: &str) -> bool {
        let Some(p) = self.get(id) else { return false };
        if p.exit.lock().unwrap().is_none() {
            platform::kill_tree(p.pid).await;
        }
        true
    }
    pub async fn stop_run(&self, run_id: &str) -> usize {
        let ids: Vec<String> = self.list().into_iter().filter(|p| p.run_id == run_id).map(|p| p.id.clone()).collect();
        for id in &ids {
            self.stop(id).await;
        }
        ids.len()
    }
    pub async fn stop_all(&self) {
        for p in self.list() {
            self.stop(&p.id).await;
        }
    }
}

pub fn render_proc(p: &Proc, tail_bytes: usize) -> String {
    let s = p.buf.lock().unwrap().render();
    let s = redact(&s);
    if s.len() > tail_bytes {
        let mut cut = s.len() - tail_bytes;
        while !s.is_char_boundary(cut) {
            cut += 1;
        }
        format!("…{}", &s[cut..])
    } else {
        s
    }
}
