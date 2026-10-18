//! Multiplexed SNP/1 client (SNP.md §5-§10).
//!
//! One persistent TLS connection carries many streams. The reader task owns the
//! receive side (flow-control accounting, dispatch, liveness); the writer task
//! serialises outbound frames. Consumers pull events from a stream and credit is
//! returned to the server only after the consumer came back for more, so a slow
//! consumer pushes back on the server (§6.2).

use super::cbor::{self, Value};
use super::error::{RemoteError, SnpError};
use super::frame::{ftype, write_frame, Frame, FrameError, FrameReader, PRE_AUTH_MAX};
use super::tls::{self, Endpoint, Pin, Trust};
use bytes::Bytes;
use hmac::{Hmac, Mac};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;
use tokio::io::{AsyncWriteExt, ReadHalf, WriteHalf};
use tokio::sync::{mpsc, watch, OwnedSemaphorePermit, Semaphore};
use tokio::time::Instant;

type HmacSha256 = Hmac<Sha256>;

pub const VERSIONS: &[u8] = &[1];
pub const OFFERED_FEATURES: &[&str] = &["events", "files", "idempotency", "reasoning", "websearch"];
/// Largest frame this client is willing to receive.
pub const CLIENT_MAX_FRAME: u32 = 16 * 1024 * 1024;
const MAX_CREDIT: u64 = (1 << 31) - 1;
const DRAIN_GRACE: Duration = Duration::from_secs(6);
const MIN_HEALTHY_UPTIME: Duration = Duration::from_secs(5);

// ------------------------------------------------------------------- auth math
pub fn token_key(api_token: &str) -> [u8; 32] {
    Sha256::digest(api_token.as_bytes()).into()
}

fn proof(key: &[u8; 32], label: &[u8], first: &[u8], second: &[u8], spki: &[u8; 32]) -> [u8; 32] {
    let mut mac = HmacSha256::new_from_slice(key).expect("hmac accepts any key length");
    mac.update(label);
    mac.update(first);
    mac.update(second);
    mac.update(spki);
    mac.finalize().into_bytes().into()
}

/// `HMAC-SHA256(K, "SNP/1 client" ‖ nonce ‖ cnonce ‖ spki)`
pub fn client_proof(key: &[u8; 32], nonce: &[u8], cnonce: &[u8], spki: &[u8; 32]) -> [u8; 32] {
    proof(key, b"SNP/1 client", nonce, cnonce, spki)
}

/// `HMAC-SHA256(K, "SNP/1 server" ‖ cnonce ‖ nonce ‖ spki)`
pub fn server_proof(key: &[u8; 32], cnonce: &[u8], nonce: &[u8], spki: &[u8; 32]) -> [u8; 32] {
    proof(key, b"SNP/1 server", cnonce, nonce, spki)
}

// --------------------------------------------------------------------- config
#[derive(Clone, Copy, Debug)]
pub struct BackoffPolicy {
    pub initial: Duration,
    pub factor: f64,
    pub cap: Duration,
    /// Relative jitter, 0.25 = ±25 %.
    pub jitter: f64,
}

impl Default for BackoffPolicy {
    fn default() -> Self {
        BackoffPolicy { initial: Duration::from_millis(500), factor: 2.0, cap: Duration::from_secs(30), jitter: 0.25 }
    }
}

impl BackoffPolicy {
    /// Delay before reconnect attempt number `attempt` (1-based). `unit` ∈ [-1, 1] scales the jitter.
    pub fn delay(&self, attempt: u32, unit: f64) -> Duration {
        let exp = self.factor.powi(attempt.saturating_sub(1).min(32) as i32);
        let base = (self.initial.as_secs_f64() * exp).min(self.cap.as_secs_f64());
        Duration::from_secs_f64((base * (1.0 + self.jitter * unit.clamp(-1.0, 1.0))).max(0.0))
    }

    fn random_delay(&self, attempt: u32) -> Duration {
        self.delay(attempt, rand::random::<f64>() * 2.0 - 1.0)
    }
}

#[derive(Clone)]
pub struct SnpConfig {
    pub endpoint: Endpoint,
    /// `Some` → pinned identity; `None` → system trust store with hostname verification.
    pub pin: Option<Pin>,
    pub token_key: [u8; 32],
    pub client_name: String,
    pub connect_timeout: Duration,
    pub handshake_timeout: Duration,
    pub backoff: BackoffPolicy,
    pub features: Vec<String>,
}

impl SnpConfig {
    pub fn new(host: &str, port: u16, pin: Option<Pin>, api_token: &str) -> Self {
        SnpConfig {
            endpoint: Endpoint { host: host.to_string(), port, server_name: None },
            pin,
            token_key: token_key(api_token),
            client_name: format!("sarness-desktop/{}", env!("CARGO_PKG_VERSION")),
            connect_timeout: Duration::from_secs(6),
            handshake_timeout: Duration::from_secs(10),
            backoff: BackoffPolicy::default(),
            features: OFFERED_FEATURES.iter().map(|s| s.to_string()).collect(),
        }
    }

    fn trust(&self) -> Trust {
        match self.pin {
            Some(p) => Trust::Pinned(p),
            None => Trust::System,
        }
    }
}

// --------------------------------------------------------------------- session
#[derive(Clone, Debug, Serialize)]
pub struct Limits {
    pub max_frame: u32,
    pub initial_window: u32,
    pub conn_window: u32,
    pub max_streams: u32,
    pub ping_interval_ms: u32,
    pub idle_timeout_ms: u32,
    pub idem_ttl_s: u32,
}

impl Limits {
    fn parse(v: &Value) -> Result<Limits, SnpError> {
        let get = |k: &str, lo: u64, hi: u64| -> Result<u32, SnpError> {
            let n = v
                .get(k)
                .and_then(Value::as_u64)
                .ok_or_else(|| SnpError::Protocol(format!("HELLO_OK limits.{k} is missing or not an integer")))?;
            if n < lo || n > hi {
                return Err(SnpError::Protocol(format!("HELLO_OK limits.{k}={n} is outside the allowed range {lo}..{hi}")));
            }
            Ok(n as u32)
        };
        let max_frame = get("max_frame", 16_384, CLIENT_MAX_FRAME as u64)?;
        let initial_window = get("initial_window", 16_384, MAX_CREDIT)?;
        let conn_window = get("conn_window", initial_window as u64, MAX_CREDIT)?;
        let max_streams = get("max_streams", 1, 1024)?;
        let ping_interval_ms = get("ping_interval_ms", 1_000, 120_000)?;
        let idle_timeout_ms = get("idle_timeout_ms", 2 * ping_interval_ms as u64, u32::MAX as u64)?;
        let idem_ttl_s = get("idem_ttl_s", 0, 86_400)?;
        Ok(Limits { max_frame, initial_window, conn_window, max_streams, ping_interval_ms, idle_timeout_ms, idem_ttl_s })
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct SessionInfo {
    pub version: u8,
    pub server: String,
    /// Features active on this connection (offered by both sides).
    pub features: Vec<String>,
    pub server_features: Vec<String>,
    pub limits: Limits,
    pub session: String,
    pub api_version: String,
    pub pin: String,
    /// HELLO → HELLO_OK round trip.
    pub latency_ms: u64,
}

impl SessionInfo {
    pub fn has(&self, feature: &str) -> bool {
        self.features.iter().any(|f| f == feature)
    }
}

// ---------------------------------------------------------------- connection
enum Incoming {
    Msg(Value, u32),
    Data(Bytes),
    End(Value),
    Error(SnpError),
}

struct StreamShared {
    id: u32,
    /// Client → server credit.
    send_credit: Semaphore,
    /// Remaining server → client credit (checked on arrival).
    recv_credit: AtomicI64,
    remote_done: AtomicBool,
}

struct StreamEntry {
    shared: Arc<StreamShared>,
    tx: mpsc::UnboundedSender<Incoming>,
    _slot: OwnedSemaphorePermit,
}

struct Table {
    next_id: u32,
    streams: HashMap<u32, StreamEntry>,
    closed: bool,
}

struct Conn {
    info: Arc<SessionInfo>,
    started: Instant,
    out: mpsc::UnboundedSender<Option<Frame>>,
    table: Mutex<Table>,
    conn_send: Semaphore,
    conn_recv: AtomicI64,
    slots: Arc<Semaphore>,
    dead: Mutex<Option<SnpError>>,
    dead_tx: watch::Sender<bool>,
    draining: AtomicBool,
    goaway: Mutex<Option<SnpError>>,
    died_at: Mutex<Option<Instant>>,
}

struct Fail {
    err: SnpError,
    /// GOAWAY code to send before closing, for protocol violations.
    goaway: Option<&'static str>,
}

fn protocol(msg: impl Into<String>) -> Fail {
    Fail { err: SnpError::Protocol(msg.into()), goaway: Some("protocol_error") }
}

impl Conn {
    fn usable(&self) -> bool {
        !self.draining.load(Ordering::Acquire) && self.dead.lock().unwrap().is_none()
    }

    fn goaway_error(&self) -> SnpError {
        self.goaway.lock().unwrap().clone().unwrap_or(SnpError::StreamClosed)
    }

    fn dead_error(&self) -> SnpError {
        self.dead.lock().unwrap().clone().unwrap_or(SnpError::StreamClosed)
    }

    fn send(&self, f: Frame) -> Result<(), SnpError> {
        self.out.send(Some(f)).map_err(|_| self.dead_error())
    }

    fn send_window(&self, stream: u32, inc: u32) {
        if inc == 0 {
            return;
        }
        if let Ok(p) = cbor::encode(&Value::map([("inc", Value::Int(inc as i64))])) {
            let _ = self.out.send(Some(Frame::new(ftype::WINDOW, stream, p)));
        }
    }

    fn grant_conn(&self, n: u32) {
        self.conn_recv.fetch_add(n as i64, Ordering::AcqRel);
        self.send_window(0, n);
    }

    fn grant_stream(&self, sh: &StreamShared, n: u32) {
        sh.recv_credit.fetch_add(n as i64, Ordering::AcqRel);
        self.send_window(sh.id, n);
    }

    fn send_goaway(&self, code: &str, message: &str) {
        let m: String = message.chars().take(256).collect();
        if let Ok(p) = cbor::encode(&Value::map([("code", Value::text(code)), ("message", Value::text(m))])) {
            let _ = self.out.send(Some(Frame::new(ftype::GOAWAY, 0, p)));
        }
    }

    /// Terminates the connection: every stream is failed with `err` (idempotent).
    fn fail(&self, err: SnpError) {
        {
            let mut d = self.dead.lock().unwrap();
            if d.is_some() {
                return;
            }
            *d = Some(err.clone());
            *self.died_at.lock().unwrap() = Some(Instant::now());
        }
        let entries: Vec<StreamEntry> = {
            let mut t = self.table.lock().unwrap();
            t.closed = true;
            t.streams.drain().map(|(_, e)| e).collect()
        };
        for e in entries {
            e.shared.send_credit.close();
            let _ = e.tx.send(Incoming::Error(err.clone()));
        }
        self.conn_send.close();
        self.slots.close();
        let _ = self.out.send(None);
        let _ = self.dead_tx.send(true);
    }

    fn remove_stream(&self, id: u32) -> Option<StreamEntry> {
        let e = self.table.lock().unwrap().streams.remove(&id);
        if let Some(e) = &e {
            e.shared.send_credit.close();
        }
        e
    }

    fn cancel_stream(&self, id: u32) {
        if self.remove_stream(id).is_some() {
            let _ = self.out.send(Some(Frame::new(ftype::CANCEL, id, Vec::new())));
        }
    }

    fn chunk_limit(&self) -> usize {
        let l = &self.info.limits;
        l.max_frame.min(l.initial_window).min(l.conn_window) as usize
    }

    /// Opens a stream: acquires a stream slot, assigns the next odd id and queues
    /// OPEN under the table lock so ids appear on the wire in increasing order.
    async fn open(self: &Arc<Self>, op: &str, args: Value, idem: Option<&str>, deadline_ms: Option<u32>) -> Result<SnpStream, SnpError> {
        if self.draining.load(Ordering::Acquire) {
            return Err(SnpError::NotProcessed);
        }
        let slot = self.slots.clone().acquire_owned().await.map_err(|_| self.dead_error())?;
        let mut m = vec![("op".to_string(), Value::text(op)), ("args".to_string(), args)];
        if let Some(i) = idem {
            m.push(("idem".into(), Value::text(i)));
        }
        if let Some(d) = deadline_ms {
            m.push(("deadline_ms".into(), Value::Int(d as i64)));
        }
        let payload = cbor::encode(&Value::Map(m)).map_err(|e| SnpError::Protocol(format!("cannot encode request: {e}")))?;
        let lim = &self.info.limits;
        if payload.len() > lim.max_frame as usize {
            return Err(SnpError::Limit(format!(
                "the request is {} bytes but the server accepts frames of at most {} bytes",
                payload.len(),
                lim.max_frame
            )));
        }
        let (tx, rx) = mpsc::unbounded_channel();
        let shared = {
            let mut t = self.table.lock().unwrap();
            if t.closed {
                return Err(self.dead_error());
            }
            if t.next_id > u32::MAX - 2 {
                self.draining.store(true, Ordering::Release);
                return Err(SnpError::NotProcessed);
            }
            let id = t.next_id;
            t.next_id += 2;
            let shared = Arc::new(StreamShared {
                id,
                send_credit: Semaphore::new(lim.initial_window as usize),
                recv_credit: AtomicI64::new(lim.initial_window as i64),
                remote_done: AtomicBool::new(false),
            });
            t.streams.insert(id, StreamEntry { shared: shared.clone(), tx, _slot: slot });
            self.out.send(Some(Frame::new(ftype::OPEN, id, payload))).map_err(|_| self.dead_error())?;
            shared
        };
        Ok(SnpStream { conn: self.clone(), shared, rx, pending: 0, finished: false })
    }

    // ---- inbound frames -------------------------------------------------
    fn on_frame(&self, f: Frame, drain: &mut Option<Instant>) -> Result<(), Fail> {
        use ftype::*;
        match f.ftype {
            PING => {
                if f.stream != 0 || f.payload.len() != 8 {
                    return Err(protocol("malformed PING"));
                }
                let _ = self.out.send(Some(Frame::new(PONG, 0, f.payload)));
                Ok(())
            }
            PONG => Ok(()),
            GOAWAY => self.on_goaway(&f, drain),
            MSG | DATA | END | ERROR | WINDOW => {
                if f.stream != 0 && f.stream % 2 == 0 {
                    return Err(protocol("frame on a server-initiated stream id"));
                }
                match f.ftype {
                    MSG | DATA => self.on_data(f),
                    END => self.on_end(f),
                    ERROR => self.on_error(f),
                    _ => self.on_window(f),
                }
            }
            HELLO | HELLO_OK | AUTH | AUTH_OK | OPEN | CANCEL => Err(protocol(format!("unexpected frame type 0x{:02x} from the server", f.ftype))),
            t => Err(protocol(format!("unknown frame type 0x{t:02x}"))),
        }
    }

    fn stream_handles(&self, id: u32) -> Option<(Arc<StreamShared>, mpsc::UnboundedSender<Incoming>)> {
        self.table.lock().unwrap().streams.get(&id).map(|e| (e.shared.clone(), e.tx.clone()))
    }

    /// Stream-level failure that does not affect the connection.
    fn stream_error(&self, id: u32, err: SnpError) {
        if let Some(e) = self.remove_stream(id) {
            e.shared.remote_done.store(true, Ordering::Release);
            let _ = e.tx.send(Incoming::Error(err));
            let _ = self.out.send(Some(Frame::new(ftype::CANCEL, id, Vec::new())));
        }
    }

    fn on_data(&self, f: Frame) -> Result<(), Fail> {
        if f.stream == 0 {
            return Err(protocol("MSG/DATA on stream 0"));
        }
        let len = f.payload.len() as i64;
        if self.conn_recv.fetch_sub(len, Ordering::AcqRel) - len < 0 {
            return Err(protocol("connection flow-control window exceeded"));
        }
        let Some((sh, tx)) = self.stream_handles(f.stream) else {
            // Late frame for a closed stream: discard, but give the connection credit back.
            self.grant_conn(len as u32);
            return Ok(());
        };
        if sh.recv_credit.fetch_sub(len, Ordering::AcqRel) - len < 0 {
            return Err(protocol("stream flow-control window exceeded"));
        }
        let inc = if f.ftype == ftype::MSG {
            match cbor::decode(&f.payload) {
                Ok(v) => Incoming::Msg(v, len as u32),
                Err(e) => {
                    self.grant_conn(len as u32);
                    self.stream_error(f.stream, SnpError::Protocol(format!("malformed message from server: {e}")));
                    return Ok(());
                }
            }
        } else {
            Incoming::Data(f.payload)
        };
        if tx.send(inc).is_err() {
            self.grant_conn(len as u32);
        }
        Ok(())
    }

    fn on_end(&self, f: Frame) -> Result<(), Fail> {
        if f.stream == 0 {
            return Err(protocol("END on stream 0"));
        }
        let result = if f.payload.is_empty() {
            Ok(Value::Null)
        } else {
            cbor::decode(&f.payload).map(|v| v.get("result").cloned().unwrap_or(Value::Null))
        };
        match result {
            Ok(v) => {
                if let Some(e) = self.remove_stream(f.stream) {
                    e.shared.remote_done.store(true, Ordering::Release);
                    let _ = e.tx.send(Incoming::End(v));
                }
            }
            Err(e) => self.stream_error(f.stream, SnpError::Protocol(format!("malformed END from server: {e}"))),
        }
        Ok(())
    }

    fn on_error(&self, f: Frame) -> Result<(), Fail> {
        let remote = match cbor::decode(&f.payload) {
            Ok(v) => RemoteError::from_value(&v),
            Err(e) => RemoteError { code: "internal".into(), status: 500, message: format!("malformed ERROR frame: {e}"), retryable: false, detail: None },
        };
        if f.stream == 0 {
            return Err(Fail { err: SnpError::Remote(remote), goaway: None });
        }
        if let Some(e) = self.remove_stream(f.stream) {
            e.shared.remote_done.store(true, Ordering::Release);
            let _ = e.tx.send(Incoming::Error(SnpError::Remote(remote)));
        }
        Ok(())
    }

    fn on_window(&self, f: Frame) -> Result<(), Fail> {
        let v = cbor::decode(&f.payload).map_err(|e| protocol(format!("malformed WINDOW: {e}")))?;
        let inc = v.get("inc").and_then(Value::as_u64).ok_or_else(|| protocol("WINDOW without inc"))?;
        if f.stream == 0 {
            if self.conn_send.available_permits() as u64 + inc > MAX_CREDIT {
                return Err(protocol("connection window above 2^31-1"));
            }
            self.conn_send.add_permits(inc as usize);
        } else if let Some((sh, _)) = self.stream_handles(f.stream) {
            if sh.send_credit.available_permits() as u64 + inc > MAX_CREDIT {
                return Err(protocol("stream window above 2^31-1"));
            }
            sh.send_credit.add_permits(inc as usize);
        }
        Ok(())
    }

    fn on_goaway(&self, f: &Frame, drain: &mut Option<Instant>) -> Result<(), Fail> {
        let v = cbor::decode(&f.payload).unwrap_or(Value::Null);
        let code = v.get("code").and_then(Value::as_str).unwrap_or("normal").to_string();
        let message = v.get("message").and_then(Value::as_str).unwrap_or("").to_string();
        let fatal = |err: SnpError| Err(Fail { err, goaway: None });
        match code.as_str() {
            "unsupported_version" => {
                let supported = v
                    .get("supported")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(|x| x.as_u64().map(|n| n as u8)).collect())
                    .unwrap_or_default();
                fatal(SnpError::UnsupportedVersion { supported })
            }
            "unauthenticated" => fatal(SnpError::Unauthenticated(message.clone())),
            "rate_limited" => fatal(SnpError::RateLimited(message.clone())),
            "protocol_error" | "frame_too_large" => fatal(SnpError::GoAway { code: code.clone(), message: message.clone() }),
            _ => {
                // normal / shutdown / idle_timeout / resource_exhausted: stop using this
                // connection, finish what was processed, retry what was not.
                self.draining.store(true, Ordering::Release);
                *self.goaway.lock().unwrap() = Some(SnpError::GoAway { code: code.clone(), message: message.clone() });
                let last = v.get("last_stream_id").and_then(Value::as_u64).map(|n| n as u32);
                if let Some(last) = last {
                    let doomed: Vec<u32> = self.table.lock().unwrap().streams.keys().copied().filter(|id| *id > last).collect();
                    for id in doomed {
                        if let Some(e) = self.remove_stream(id) {
                            let _ = e.tx.send(Incoming::Error(SnpError::NotProcessed));
                        }
                    }
                }
                *drain = Some(Instant::now() + DRAIN_GRACE);
                Ok(())
            }
        }
    }
}

async fn reader_loop(conn: Arc<Conn>, mut rd: FrameReader<ReadHalf<tls::TlsStream>>) {
    let lim = &conn.info.limits;
    let ping_iv = Duration::from_millis(lim.ping_interval_ms as u64);
    let idle = Duration::from_millis(lim.idle_timeout_ms as u64);
    let mut last_rx = Instant::now();
    let mut pinged = false;
    let mut dead_rx = conn.dead_tx.subscribe();
    let mut drain: Option<Instant> = None;
    loop {
        let ping_at = last_rx + ping_iv;
        let idle_at = last_rx + idle;
        let drain_at = drain;
        tokio::select! {
            r = rd.next() => match r {
                Ok(f) => {
                    last_rx = Instant::now();
                    pinged = false;
                    if let Err(fail) = conn.on_frame(f, &mut drain) {
                        if let Some(code) = fail.goaway {
                            conn.send_goaway(code, &fail.err.to_string());
                        }
                        conn.fail(fail.err);
                        return;
                    }
                    if drain.is_some() && conn.table.lock().unwrap().streams.is_empty() {
                        conn.fail(conn.goaway_error());
                        return;
                    }
                }
                Err(FrameError::Closed) => { conn.fail(SnpError::ConnectionLost("the server closed the connection".into())); return; }
                Err(FrameError::Truncated) => { conn.fail(SnpError::ConnectionLost("the server closed the connection mid-frame".into())); return; }
                Err(FrameError::Io(e)) => { conn.fail(SnpError::ConnectionLost(e.to_string())); return; }
                Err(e @ FrameError::TooLarge { .. }) => {
                    conn.send_goaway("frame_too_large", "frame exceeds the negotiated maximum");
                    conn.fail(SnpError::Protocol(e.to_string()));
                    return;
                }
                Err(e) => {
                    conn.send_goaway("protocol_error", &e.to_string());
                    conn.fail(SnpError::Protocol(e.to_string()));
                    return;
                }
            },
            _ = tokio::time::sleep_until(ping_at), if !pinged => {
                let _ = conn.out.send(Some(Frame::new(ftype::PING, 0, rand::random::<[u8; 8]>().to_vec())));
                pinged = true;
            }
            _ = tokio::time::sleep_until(idle_at) => {
                conn.send_goaway("idle_timeout", "no data received");
                conn.fail(SnpError::ConnectionLost(format!("no data from the server for {} s", idle.as_secs())));
                return;
            }
            _ = tokio::time::sleep_until(drain_at.unwrap_or(last_rx)), if drain_at.is_some() => {
                conn.fail(conn.goaway_error());
                return;
            }
            _ = dead_rx.changed() => return,
        }
    }
}

async fn writer_loop(conn: Arc<Conn>, mut rx: mpsc::UnboundedReceiver<Option<Frame>>, mut wr: WriteHalf<tls::TlsStream>) {
    while let Some(item) = rx.recv().await {
        match item {
            Some(f) => {
                if let Err(e) = write_frame(&mut wr, &f).await {
                    conn.fail(SnpError::ConnectionLost(e.to_string()));
                    break;
                }
            }
            None => break,
        }
    }
    let _ = tokio::time::timeout(Duration::from_secs(1), wr.shutdown()).await;
}

// -------------------------------------------------------------------- streams
#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Msg(Value),
    Data(Bytes),
    /// Terminal: the server's `END{result}` result (null when absent).
    End(Value),
}

pub struct SnpStream {
    conn: Arc<Conn>,
    shared: Arc<StreamShared>,
    rx: mpsc::UnboundedReceiver<Incoming>,
    /// Consumed but not yet returned to the server.
    pending: u32,
    finished: bool,
}

impl SnpStream {
    pub fn id(&self) -> u32 {
        self.shared.id
    }

    pub fn session(&self) -> &SessionInfo {
        &self.conn.info
    }

    fn flush_credit(&mut self) {
        if self.pending == 0 {
            return;
        }
        let n = std::mem::take(&mut self.pending);
        self.conn.grant_conn(n);
        if !self.shared.remote_done.load(Ordering::Acquire) {
            self.conn.grant_stream(&self.shared, n);
        }
    }

    /// Next event. Window credit for everything consumed so far is returned to the
    /// server only when the caller asks for more (backpressure), or once half a
    /// window has been consumed.
    pub async fn recv(&mut self) -> Result<Event, SnpError> {
        if self.finished {
            return Err(SnpError::StreamClosed);
        }
        if self.rx.is_empty() {
            self.flush_credit();
        }
        match self.rx.recv().await {
            None => {
                self.finished = true;
                Err(self.conn.dead_error())
            }
            Some(Incoming::Msg(v, n)) => {
                self.pending = self.pending.saturating_add(n);
                self.maybe_flush();
                Ok(Event::Msg(v))
            }
            Some(Incoming::Data(b)) => {
                self.pending = self.pending.saturating_add(b.len() as u32);
                self.maybe_flush();
                Ok(Event::Data(b))
            }
            Some(Incoming::End(v)) => {
                self.finished = true;
                self.flush_credit();
                Ok(Event::End(v))
            }
            Some(Incoming::Error(e)) => {
                self.finished = true;
                self.flush_credit();
                Err(e)
            }
        }
    }

    fn maybe_flush(&mut self) {
        if self.pending >= self.conn.info.limits.initial_window / 2 {
            self.flush_credit();
        }
    }

    async fn acquire_credit(&self, n: u32) -> Result<(), SnpError> {
        let closed = || {
            if self.conn.dead.lock().unwrap().is_some() {
                self.conn.dead_error()
            } else {
                SnpError::StreamClosed
            }
        };
        let sp = self.shared.send_credit.acquire_many(n).await.map_err(|_| closed())?;
        let cp = self.conn.conn_send.acquire_many(n).await.map_err(|_| closed())?;
        sp.forget();
        cp.forget();
        Ok(())
    }

    /// Sends a MSG (flow-controlled).
    pub async fn send_msg(&self, v: &Value) -> Result<(), SnpError> {
        let payload = cbor::encode(v).map_err(|e| SnpError::Protocol(format!("cannot encode message: {e}")))?;
        let limit = self.conn.chunk_limit();
        if payload.len() > limit {
            return Err(SnpError::Limit(format!("message of {} bytes exceeds the negotiated limit of {limit}", payload.len())));
        }
        self.acquire_credit(payload.len() as u32).await?;
        self.conn.send(Frame::new(ftype::MSG, self.shared.id, payload))
    }

    /// Sends DATA, split into frames no larger than the negotiated limits (flow-controlled).
    pub async fn send_data(&self, data: Bytes) -> Result<(), SnpError> {
        let chunk = self.conn.chunk_limit();
        let mut off = 0;
        while off < data.len() {
            let end = (off + chunk).min(data.len());
            self.acquire_credit((end - off) as u32).await?;
            self.conn.send(Frame::new(ftype::DATA, self.shared.id, data.slice(off..end)))?;
            off = end;
        }
        Ok(())
    }

    /// Half-closes the client side (`END`).
    pub fn end(&self) -> Result<(), SnpError> {
        if self.shared.remote_done.load(Ordering::Acquire) {
            return Err(SnpError::StreamClosed);
        }
        self.conn.send(Frame::new(ftype::END, self.shared.id, Vec::new()))
    }

    /// Requests termination (`CANCEL`) and releases the stream.
    pub fn cancel(&mut self) {
        self.finished = true;
        self.conn.cancel_stream(self.shared.id);
    }
}

impl Drop for SnpStream {
    fn drop(&mut self) {
        self.rx.close();
        let mut n = std::mem::take(&mut self.pending);
        while let Ok(inc) = self.rx.try_recv() {
            match inc {
                Incoming::Msg(_, l) => n = n.saturating_add(l),
                Incoming::Data(b) => n = n.saturating_add(b.len() as u32),
                _ => {}
            }
        }
        if n > 0 {
            self.conn.grant_conn(n);
        }
        // No-op when the server already ended the stream.
        self.conn.cancel_stream(self.shared.id);
    }
}

// ------------------------------------------------------------------ handshake
fn map_frame_error(e: FrameError) -> SnpError {
    match e {
        FrameError::Closed | FrameError::Truncated => SnpError::ConnectionLost("the server closed the connection during the handshake".into()),
        FrameError::Io(e) => SnpError::ConnectionLost(e.to_string()),
        other => SnpError::Protocol(other.to_string()),
    }
}

fn goaway_error(f: &Frame) -> SnpError {
    let v = cbor::decode(&f.payload).unwrap_or(Value::Null);
    let code = v.get("code").and_then(Value::as_str).unwrap_or("").to_string();
    let message = v.get("message").and_then(Value::as_str).unwrap_or("").to_string();
    match code.as_str() {
        "unsupported_version" => SnpError::UnsupportedVersion {
            supported: v
                .get("supported")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(|x| x.as_u64().map(|n| n as u8)).collect())
                .unwrap_or_default(),
        },
        "unauthenticated" => SnpError::Unauthenticated(message),
        "rate_limited" => SnpError::RateLimited(message),
        _ => SnpError::GoAway { code, message },
    }
}

fn text_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default()
}

async fn handshake(
    rd: &mut FrameReader<ReadHalf<tls::TlsStream>>,
    wr: &mut WriteHalf<tls::TlsStream>,
    cfg: &SnpConfig,
    pin: &Pin,
) -> Result<SessionInfo, SnpError> {
    let enc = |v: Value| cbor::encode(&v).map_err(|e| SnpError::Protocol(e.to_string()));
    let hello = Value::map([
        ("versions", Value::Array(VERSIONS.iter().map(|v| Value::Int(*v as i64)).collect())),
        ("client", Value::text(cfg.client_name.chars().take(64).collect::<String>())),
        ("features", Value::Array(cfg.features.iter().take(32).map(Value::text).collect())),
        ("max_frame", Value::Int(CLIENT_MAX_FRAME as i64)),
    ]);
    let t0 = Instant::now();
    write_frame(wr, &Frame::new(ftype::HELLO, 0, enc(hello)?)).await?;
    let f = rd.next().await.map_err(map_frame_error)?;
    let latency_ms = t0.elapsed().as_millis() as u64;
    let ok = match f.ftype {
        ftype::HELLO_OK if f.stream == 0 => cbor::decode(&f.payload).map_err(|e| SnpError::Protocol(format!("malformed HELLO_OK: {e}")))?,
        ftype::GOAWAY => return Err(goaway_error(&f)),
        t => return Err(SnpError::Protocol(format!("expected HELLO_OK, got frame type 0x{t:02x}"))),
    };
    let version = ok.get("version").and_then(Value::as_u64).ok_or_else(|| SnpError::Protocol("HELLO_OK without version".into()))?;
    if version > 255 || !VERSIONS.contains(&(version as u8)) {
        return Err(SnpError::UnsupportedVersion { supported: vec![version.min(255) as u8] });
    }
    let nonce = ok
        .get("nonce")
        .and_then(Value::as_bytes)
        .filter(|n| n.len() == 32)
        .ok_or_else(|| SnpError::Protocol("HELLO_OK nonce must be 32 bytes".into()))?
        .to_vec();
    if !text_list(ok.get("auth")).iter().any(|m| m == "hmac-sha256") {
        return Err(SnpError::Protocol("the server offers no supported authentication method".into()));
    }
    let limits = Limits::parse(ok.get("limits").ok_or_else(|| SnpError::Protocol("HELLO_OK without limits".into()))?)?;
    let server_features = text_list(ok.get("features"));
    let features: Vec<String> = cfg.features.iter().filter(|f| server_features.contains(f)).cloned().collect();

    let cnonce = rand::random::<[u8; 16]>();
    let client = client_proof(&cfg.token_key, &nonce, &cnonce, &pin.0);
    let auth = Value::map([
        ("method", Value::text("hmac-sha256")),
        ("cnonce", Value::Bytes(cnonce.to_vec())),
        ("proof", Value::Bytes(client.to_vec())),
    ]);
    write_frame(wr, &Frame::new(ftype::AUTH, 0, enc(auth)?)).await?;
    let f = rd.next().await.map_err(map_frame_error)?;
    let ok2 = match f.ftype {
        ftype::AUTH_OK if f.stream == 0 => cbor::decode(&f.payload).map_err(|e| SnpError::Protocol(format!("malformed AUTH_OK: {e}")))?,
        ftype::GOAWAY => return Err(goaway_error(&f)),
        t => return Err(SnpError::Protocol(format!("expected AUTH_OK, got frame type 0x{t:02x}"))),
    };
    // Verify the server's proof in constant time (Mac::verify_slice).
    let their = ok2.get("proof").and_then(Value::as_bytes).ok_or(SnpError::ServerProof)?;
    let mut mac = HmacSha256::new_from_slice(&cfg.token_key).expect("hmac key");
    mac.update(b"SNP/1 server");
    mac.update(&cnonce);
    mac.update(&nonce);
    mac.update(&pin.0);
    mac.verify_slice(their).map_err(|_| SnpError::ServerProof)?;

    rd.set_max(limits.max_frame);
    Ok(SessionInfo {
        version: version as u8,
        server: ok.get("server").and_then(Value::as_str).unwrap_or("").chars().take(128).collect(),
        features,
        server_features,
        limits,
        session: ok2.get("session").and_then(Value::as_str).unwrap_or("").to_string(),
        api_version: ok2.get("api_version").and_then(Value::as_str).unwrap_or("").to_string(),
        pin: pin.to_string(),
        latency_ms,
    })
}

async fn establish(cfg: &SnpConfig) -> Result<Arc<Conn>, SnpError> {
    let (tls, pin) = tls::connect(&cfg.endpoint, &cfg.trust(), cfg.connect_timeout).await?;
    let (rd, mut wr) = tokio::io::split(tls);
    let mut rd = FrameReader::new(rd, PRE_AUTH_MAX);
    let info = match tokio::time::timeout(cfg.handshake_timeout, handshake(&mut rd, &mut wr, cfg, &pin)).await {
        Err(_) => return Err(SnpError::Timeout { stage: "handshake".into() }),
        Ok(r) => r?,
    };
    let lim = info.limits.clone();
    let (out_tx, out_rx) = mpsc::unbounded_channel();
    let (dead_tx, _) = watch::channel(false);
    let conn = Arc::new(Conn {
        info: Arc::new(info),
        started: Instant::now(),
        out: out_tx,
        table: Mutex::new(Table { next_id: 1, streams: HashMap::new(), closed: false }),
        conn_send: Semaphore::new(lim.conn_window as usize),
        conn_recv: AtomicI64::new(lim.conn_window as i64),
        slots: Arc::new(Semaphore::new(lim.max_streams as usize)),
        dead: Mutex::new(None),
        dead_tx,
        draining: AtomicBool::new(false),
        goaway: Mutex::new(None),
        died_at: Mutex::new(None),
    });
    tokio::spawn(reader_loop(conn.clone(), rd));
    tokio::spawn(writer_loop(conn.clone(), out_rx, wr));
    Ok(conn)
}

// --------------------------------------------------------------------- client
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Retry {
    /// GET-equivalent: always safe to re-send.
    Safe,
    /// Mutating: re-send only with an idempotency key.
    Idem,
    /// Never re-send automatically.
    No,
}

#[derive(Clone, Debug)]
pub struct CallOpts {
    pub retry: Retry,
    pub deadline: Option<Duration>,
    pub attempts: u32,
}

impl CallOpts {
    pub fn new(retry: Retry) -> Self {
        CallOpts { retry, deadline: None, attempts: 3 }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ClientStatus {
    /// idle | connected | backoff | failed
    pub state: String,
    pub error: Option<String>,
    pub error_kind: Option<String>,
    pub fatal: bool,
}

struct State {
    conn: Option<Arc<Conn>>,
    fatal: Option<SnpError>,
    attempts: u32,
    next_allowed: Instant,
    last_err: Option<SnpError>,
}

struct Inner {
    cfg: RwLock<SnpConfig>,
    state: tokio::sync::Mutex<State>,
    status: Mutex<ClientStatus>,
    connects: AtomicU64,
}

#[derive(Clone)]
pub struct SnpClient {
    inner: Arc<Inner>,
}

impl Drop for Inner {
    fn drop(&mut self) {
        if let Some(c) = self.state.get_mut().conn.take() {
            c.fail(SnpError::Cancelled);
        }
    }
}

impl SnpClient {
    pub fn new(cfg: SnpConfig) -> Self {
        SnpClient {
            inner: Arc::new(Inner {
                cfg: RwLock::new(cfg),
                state: tokio::sync::Mutex::new(State { conn: None, fatal: None, attempts: 0, next_allowed: Instant::now(), last_err: None }),
                status: Mutex::new(ClientStatus { state: "idle".into(), error: None, error_kind: None, fatal: false }),
                connects: AtomicU64::new(0),
            }),
        }
    }

    /// Points the client at a new address (e.g. after an SSH tunnel restarted on another local port).
    pub fn set_endpoint(&self, host: &str, port: u16) {
        let mut c = self.inner.cfg.write().unwrap();
        c.endpoint.host = host.to_string();
        c.endpoint.port = port;
    }

    /// Number of connections established so far (diagnostics, tests).
    pub fn connect_count(&self) -> u64 {
        self.inner.connects.load(Ordering::Relaxed)
    }

    pub fn status(&self) -> ClientStatus {
        self.inner.status.lock().unwrap().clone()
    }

    fn set_status(&self, state: &str, err: Option<&SnpError>) {
        *self.inner.status.lock().unwrap() = ClientStatus {
            state: state.into(),
            error: err.map(|e| e.to_string()),
            error_kind: err.map(|e| e.kind().to_string()),
            fatal: err.map(|e| e.is_fatal()).unwrap_or(false),
        };
    }

    /// Forgets a sticky failure and any back-off, and drops the current connection
    /// (explicit user-initiated reconnect).
    pub async fn reset(&self) {
        let mut st = self.inner.state.lock().await;
        st.fatal = None;
        st.attempts = 0;
        st.next_allowed = Instant::now();
        st.last_err = None;
        if let Some(c) = st.conn.take() {
            c.fail(SnpError::Cancelled);
        }
        self.set_status("idle", None);
    }

    pub async fn close(&self) {
        self.reset().await;
    }

    /// Negotiated session of the live connection, if any.
    pub async fn info(&self) -> Option<Arc<SessionInfo>> {
        let st = self.inner.state.lock().await;
        st.conn.as_ref().filter(|c| c.usable()).map(|c| c.info.clone())
    }

    /// Connects (or reuses the live connection) and returns the negotiated session.
    pub async fn connect(&self) -> Result<Arc<SessionInfo>, SnpError> {
        Ok(self.get_conn().await?.info.clone())
    }

    async fn get_conn(&self) -> Result<Arc<Conn>, SnpError> {
        let mut st = self.inner.state.lock().await;
        if let Some(e) = &st.fatal {
            return Err(e.clone());
        }
        if let Some(c) = st.conn.clone() {
            if c.usable() {
                return Ok(c);
            }
            // The connection ended. One that died right after the handshake counts as a failure and the
            // back-off window starts when it died, not when we noticed.
            st.conn = None;
            let died = *c.died_at.lock().unwrap();
            match died {
                Some(d) if d.duration_since(c.started) < MIN_HEALTHY_UPTIME => {
                    let cfg = self.inner.cfg.read().unwrap().clone();
                    st.attempts += 1;
                    st.next_allowed = d + cfg.backoff.random_delay(st.attempts);
                    st.last_err = Some(c.dead.lock().unwrap().clone().unwrap_or(SnpError::StreamClosed));
                }
                _ => st.attempts = 0,
            }
        }
        let now = Instant::now();
        if st.next_allowed > now {
            let last = st.last_err.clone().unwrap_or(SnpError::Io("previous connection attempt failed".into()));
            return Err(SnpError::Backoff { retry_in: st.next_allowed - now, last: Box::new(last) });
        }
        let cfg = self.inner.cfg.read().unwrap().clone();
        match establish(&cfg).await {
            Ok(c) => {
                st.conn = Some(c.clone());
                st.last_err = None;
                self.inner.connects.fetch_add(1, Ordering::Relaxed);
                self.set_status("connected", None);
                Ok(c)
            }
            Err(e) => {
                if e.is_fatal() {
                    st.fatal = Some(e.clone());
                    self.set_status("failed", Some(&e));
                } else {
                    st.attempts += 1;
                    st.next_allowed = Instant::now() + cfg.backoff.random_delay(st.attempts);
                    st.last_err = Some(e.clone());
                    self.set_status("backoff", Some(&e));
                }
                Err(e)
            }
        }
    }

    /// Opens a raw stream on the live connection (connecting first when needed).
    pub async fn open(&self, op: &str, args: Value) -> Result<SnpStream, SnpError> {
        let conn = self.get_conn().await?;
        conn.open(op, args, None, None).await
    }

    /// Unary operation with deadline, idempotency key and transparent retry after reconnect.
    pub async fn call(&self, op: &str, args: Value, opts: CallOpts) -> Result<Value, SnpError> {
        // One key for the logical operation: re-used verbatim on every retry (§8).
        let idem = (opts.retry == Retry::Idem).then(|| uuid::Uuid::new_v4().simple().to_string());
        let started = Instant::now();
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let conn = match self.get_conn().await {
                Ok(c) => c,
                Err(SnpError::Backoff { retry_in, last }) if attempt > 1 => {
                    // Internal retry: wait out the back-off instead of failing.
                    if opts.deadline.map(|d| started.elapsed() + retry_in > d).unwrap_or(false) {
                        return Err(*last);
                    }
                    tokio::time::sleep(retry_in).await;
                    continue;
                }
                Err(e) => return Err(e),
            };
            let attach = idem.as_deref().filter(|_| conn.info.has("idempotency"));
            // The first attempt gets the full deadline; retries only what is left of it.
            let remaining = opts.deadline.map(|d| if attempt == 1 { d } else { d.saturating_sub(started.elapsed()) });
            if remaining == Some(Duration::ZERO) {
                return Err(SnpError::DeadlineExceeded);
            }
            let res = attempt_once(&conn, op, args.clone(), attach, remaining).await;
            let e = match res {
                Ok(v) => return Ok(v),
                Err(e) => e,
            };
            let may_retry = match e {
                SnpError::NotProcessed => true,
                _ => e.is_retryable_transport() && (opts.retry == Retry::Safe || (opts.retry == Retry::Idem && attach.is_some())),
            };
            if !may_retry || attempt >= opts.attempts.max(1) {
                return Err(e);
            }
            let cfg = self.inner.cfg.read().unwrap().clone();
            tokio::time::sleep(cfg.backoff.random_delay(attempt).min(Duration::from_millis(500))).await;
        }
    }

    /// Subscribes to engine events. With `auto_resubscribe` the subscription is
    /// re-established (with back-off) after a reconnect.
    pub async fn subscribe_events(&self, topics: Vec<String>, auto_resubscribe: bool) -> Result<EventSubscription, SnpError> {
        let mut sub = EventSubscription { client: self.clone(), topics, stream: None, auto: auto_resubscribe, failures: 0, had_stream: false, ended: false };
        sub.stream = Some(self.open_events(&sub.topics).await?);
        sub.had_stream = true;
        Ok(sub)
    }

    async fn open_events(&self, topics: &[String]) -> Result<SnpStream, SnpError> {
        let conn = self.get_conn().await?;
        if !conn.info.has("events") {
            return Err(SnpError::Protocol("the server does not offer the events feature".into()));
        }
        let args = if topics.is_empty() {
            Value::Map(vec![])
        } else {
            Value::map([("topics", Value::Array(topics.iter().map(Value::text).collect()))])
        };
        conn.open("event.subscribe", args, None, None).await
    }
}

async fn attempt_once(conn: &Arc<Conn>, op: &str, args: Value, idem: Option<&str>, deadline: Option<Duration>) -> Result<Value, SnpError> {
    let deadline_ms = deadline.map(|d| d.as_millis().min(u32::MAX as u128) as u32);
    let mut s = conn.open(op, args, idem, deadline_ms).await?;
    let run = async {
        loop {
            match s.recv().await? {
                Event::End(v) => return Ok(v),
                _ => continue,
            }
        }
    };
    match deadline {
        None => run.await,
        Some(d) => match tokio::time::timeout(d + Duration::from_secs(5), run).await {
            Ok(r) => r,
            Err(_) => {
                s.cancel();
                Err(SnpError::DeadlineExceeded)
            }
        },
    }
}

// --------------------------------------------------------------- subscriptions
#[derive(Debug, Clone, PartialEq)]
pub enum SubEvent {
    /// One engine event (`{type, ts, data}`; the first one is `hello`).
    Event(Value),
    /// The subscription was lost; with auto-resubscribe the next call re-establishes it.
    Disconnected(SnpError),
    /// The subscription was re-established after a reconnect.
    Resubscribed,
    /// Unrecoverable (authentication / identity failure).
    Fatal(SnpError),
}

pub struct EventSubscription {
    client: SnpClient,
    topics: Vec<String>,
    stream: Option<SnpStream>,
    auto: bool,
    failures: u32,
    had_stream: bool,
    ended: bool,
}

impl EventSubscription {
    pub async fn next(&mut self) -> SubEvent {
        loop {
            if self.stream.is_none() {
                if !self.auto || self.ended {
                    return SubEvent::Disconnected(SnpError::StreamClosed);
                }
                match self.client.open_events(&self.topics).await {
                    Ok(s) => {
                        self.stream = Some(s);
                        self.failures = 0;
                        self.had_stream = true;
                        return SubEvent::Resubscribed;
                    }
                    Err(e) if e.is_fatal() => {
                        self.ended = true;
                        return SubEvent::Fatal(e);
                    }
                    Err(e) => {
                        self.failures += 1;
                        let wait = match &e {
                            SnpError::Backoff { retry_in, .. } => *retry_in,
                            _ => self.client.inner.cfg.read().unwrap().backoff.random_delay(self.failures),
                        };
                        tokio::time::sleep(wait).await;
                        continue;
                    }
                }
            }
            let res = self.stream.as_mut().unwrap().recv().await;
            match res {
                Ok(Event::Msg(v)) => return SubEvent::Event(v),
                Ok(Event::Data(_)) => continue,
                Ok(Event::End(_)) => {
                    self.stream = None;
                    return SubEvent::Disconnected(SnpError::StreamClosed);
                }
                Err(e) => {
                    self.stream = None;
                    if e.is_fatal() {
                        self.ended = true;
                        return SubEvent::Fatal(e);
                    }
                    return SubEvent::Disconnected(e);
                }
            }
        }
    }

    /// Adds / removes topics on the live subscription (and remembers them for re-subscription).
    pub async fn update_topics(&mut self, subscribe: Vec<String>, unsubscribe: Vec<String>) -> Result<(), SnpError> {
        for t in &subscribe {
            if !self.topics.contains(t) {
                self.topics.push(t.clone());
            }
        }
        self.topics.retain(|t| !unsubscribe.contains(t));
        let Some(s) = self.stream.as_ref() else { return Ok(()) };
        let arr = |v: &[String]| Value::Array(v.iter().map(Value::text).collect());
        s.send_msg(&Value::map([("subscribe", arr(&subscribe)), ("unsubscribe", arr(&unsubscribe))])).await
    }

    pub fn is_established(&self) -> bool {
        self.stream.is_some() && self.had_stream
    }
}
