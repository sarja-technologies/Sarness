//! In-process fake SNP server for the client tests.
//!
//! Deliberately independent of `snp::client` / `snp::frame` / `snp::tls`: it has its
//! own frame reader and writer, its own send/receive credit bookkeeping, its own TLS
//! setup (tokio-rustls + an rcgen self-signed certificate) and verifies the client's
//! AUTH proof with the `hmac` crate directly. Only the CBOR codec is shared (it has
//! its own conformance vectors in `tests.rs`).

use crate::snp::cbor::{self, Value};
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, mpsc, watch};
use tokio_rustls::TlsAcceptor;

pub const TOKEN: &str = "sk-test-token";
type HmacSha256 = Hmac<Sha256>;

pub fn k_of(token: &str) -> [u8; 32] {
    Sha256::digest(token.as_bytes()).into()
}

// ----------------------------------------------------------------- identity
#[derive(Clone)]
pub struct Identity {
    pub cfg: Arc<rustls::ServerConfig>,
    pub pin_hex: String,
    pub spki: [u8; 32],
    pub cert_der: Vec<u8>,
}

impl Identity {
    pub fn pin(&self) -> String {
        format!("sha256:{}", self.pin_hex)
    }
}

pub fn identity_with(alg: &'static rcgen::SignatureAlgorithm, tls12_only: bool, alpn: bool) -> Identity {
    let key = rcgen::KeyPair::generate_for(alg).unwrap();
    let params = rcgen::CertificateParams::new(vec!["localhost".to_string(), "127.0.0.1".to_string()]).unwrap();
    let cert = params.self_signed(&key).unwrap();
    let der = cert.der().clone();
    let spki: [u8; 32] = Sha256::digest(key.public_key_der()).into();
    let kd = rustls::pki_types::PrivateKeyDer::Pkcs8(rustls::pki_types::PrivatePkcs8KeyDer::from(key.serialize_der()));
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let versions: &[&'static rustls::SupportedProtocolVersion] =
        if tls12_only { &[&rustls::version::TLS12] } else { &[&rustls::version::TLS13] };
    let mut cfg = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(versions)
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(vec![der.clone()], kd)
        .unwrap();
    if alpn {
        cfg.alpn_protocols = vec![b"snp/1".to_vec()];
    }
    Identity { cfg: Arc::new(cfg), pin_hex: hex::encode(spki), spki, cert_der: der.to_vec() }
}

pub fn identity() -> Identity {
    identity_with(&rcgen::PKCS_ECDSA_P256_SHA256, false, true)
}

// -------------------------------------------------------------------- knobs
#[derive(Clone, Debug)]
pub struct LimitsCfg {
    pub max_frame: u32,
    pub initial_window: u32,
    pub conn_window: u32,
    pub max_streams: u32,
    pub ping_interval_ms: u32,
    pub idle_timeout_ms: u32,
    pub idem_ttl_s: u32,
}

impl Default for LimitsCfg {
    fn default() -> Self {
        LimitsCfg { max_frame: 65_536, initial_window: 65_536, conn_window: 262_144, max_streams: 16, ping_interval_ms: 15_000, idle_timeout_ms: 45_000, idem_ttl_s: 600 }
    }
}

#[derive(Clone, Debug)]
pub struct OpenRec {
    pub conn: u64,
    pub stream: u32,
    pub op: String,
    pub args: Value,
    pub idem: Option<String>,
    pub deadline_ms: Option<u64>,
}

pub struct Knobs {
    pub limits: Mutex<LimitsCfg>,
    pub features: Mutex<Vec<String>>,
    // switches
    pub bad_server_proof: AtomicBool,
    pub reject_auth: AtomicBool,
    pub unsupported_version: AtomicBool,
    pub mute: AtomicBool,
    pub oversize_hello_ok: AtomicBool,
    pub garbage_hello_ok: AtomicBool,
    pub missing_nonce: AtomicBool,
    pub out_of_range_limits: AtomicBool,
    pub max_frame_override_dishonest: AtomicBool,
    // counters / one-shot actions
    pub drop_next: AtomicU64,
    pub goaway_next: AtomicU64,
    pub accepts: AtomicU64,
    pub tls_ok: AtomicU64,
    pub app_frames: AtomicU64,
    pub conns_with_app_frames: AtomicU64,
    pub blob_sent: AtomicU64,
    pub blob_done: AtomicBool,
    pub upload_bytes: AtomicU64,
    pub upload_cancelled: AtomicBool,
    pub upload_eof: AtomicBool,
    pub upload_done: AtomicBool,
    /// 0 = unlimited; otherwise `file.upload` answers `too_large` beyond this many octets.
    pub upload_limit: AtomicU64,
    pub sink_delay_ms: AtomicU64,
    pub client_overran_credit: AtomicBool,
    pub pings_rx: AtomicU64,
    pub order_violation: AtomicBool,
    // logs
    pub opens: Mutex<Vec<OpenRec>>,
    pub cancels: Mutex<Vec<u32>>,
    pub goaways_rx: Mutex<Vec<String>>,
    pub pongs_rx: Mutex<Vec<Vec<u8>>>,
    pub ctrl_msgs: Mutex<Vec<Value>>,
    pub events: broadcast::Sender<Value>,
    pub kill: watch::Sender<u64>,
    /// Programmable replies for the generic `script` handlers (op -> events).
    pub scripts: Mutex<HashMap<String, Script>>,
}

#[derive(Clone, Debug)]
pub enum Reply {
    Msg(Value),
    End(Value),
    Error { code: String, status: u16, message: String, retryable: bool, detail: Option<Value> },
    /// Wait for the client's CANCEL, then reply `cancelled`.
    WaitCancel,
}

#[derive(Clone, Debug, Default)]
pub struct Script {
    pub replies: Vec<Reply>,
}

impl Knobs {
    pub fn new() -> Arc<Knobs> {
        Arc::new(Knobs {
            limits: Mutex::new(LimitsCfg::default()),
            features: Mutex::new(["events", "files", "idempotency", "reasoning", "websearch"].iter().map(|s| s.to_string()).collect()),
            bad_server_proof: AtomicBool::new(false),
            reject_auth: AtomicBool::new(false),
            unsupported_version: AtomicBool::new(false),
            mute: AtomicBool::new(false),
            oversize_hello_ok: AtomicBool::new(false),
            garbage_hello_ok: AtomicBool::new(false),
            missing_nonce: AtomicBool::new(false),
            out_of_range_limits: AtomicBool::new(false),
            max_frame_override_dishonest: AtomicBool::new(false),
            drop_next: AtomicU64::new(0),
            goaway_next: AtomicU64::new(0),
            accepts: AtomicU64::new(0),
            tls_ok: AtomicU64::new(0),
            app_frames: AtomicU64::new(0),
            conns_with_app_frames: AtomicU64::new(0),
            blob_sent: AtomicU64::new(0),
            blob_done: AtomicBool::new(false),
            upload_bytes: AtomicU64::new(0),
            upload_cancelled: AtomicBool::new(false),
            upload_eof: AtomicBool::new(false),
            upload_done: AtomicBool::new(false),
            upload_limit: AtomicU64::new(0),
            sink_delay_ms: AtomicU64::new(0),
            client_overran_credit: AtomicBool::new(false),
            pings_rx: AtomicU64::new(0),
            order_violation: AtomicBool::new(false),
            opens: Mutex::new(vec![]),
            cancels: Mutex::new(vec![]),
            goaways_rx: Mutex::new(vec![]),
            pongs_rx: Mutex::new(vec![]),
            ctrl_msgs: Mutex::new(vec![]),
            events: broadcast::channel(64).0,
            kill: watch::channel(0).0,
            scripts: Mutex::new(HashMap::new()),
        })
    }

    pub fn opens_of(&self, op: &str) -> Vec<OpenRec> {
        self.opens.lock().unwrap().iter().filter(|o| o.op == op).cloned().collect()
    }

    pub fn script(&self, op: &str, replies: Vec<Reply>) {
        self.scripts.lock().unwrap().insert(op.into(), Script { replies });
    }

    /// Abruptly closes every live connection.
    pub fn kill_all(&self) {
        self.kill.send_modify(|g| *g += 1);
    }

    pub fn limits(&self, f: impl FnOnce(&mut LimitsCfg)) {
        f(&mut self.limits.lock().unwrap());
    }
}

// -------------------------------------------------------------- raw framing
pub fn frame(t: u8, sid: u32, payload: &[u8]) -> Vec<u8> {
    let mut v = vec![0x53, 0x4e, t, 0];
    v.extend_from_slice(&sid.to_be_bytes());
    v.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    v.extend_from_slice(payload);
    v
}

async fn read_frame<R: AsyncReadExt + Unpin>(r: &mut R) -> Option<(u8, u32, Vec<u8>)> {
    let mut h = [0u8; 12];
    r.read_exact(&mut h).await.ok()?;
    assert_eq!(&h[..2], b"SN", "client sent a bad magic");
    assert_eq!(h[3], 0, "client sent non-zero flags");
    let sid = u32::from_be_bytes(h[4..8].try_into().unwrap());
    let len = u32::from_be_bytes(h[8..12].try_into().unwrap()) as usize;
    let mut p = vec![0u8; len];
    r.read_exact(&mut p).await.ok()?;
    Some((h[2], sid, p))
}

pub fn m(items: Vec<(&str, Value)>) -> Value {
    Value::map(items)
}

// ----------------------------------------------------------------- the server
pub struct Server {
    pub addr: SocketAddr,
    pub identity: Identity,
    pub knobs: Arc<Knobs>,
    task: tokio::task::JoinHandle<()>,
}

impl Server {
    pub fn port(&self) -> u16 {
        self.addr.port()
    }
    pub fn pin(&self) -> String {
        self.identity.pin()
    }
    /// Stops accepting and kills live connections.
    pub fn stop(&self) {
        self.task.abort();
        self.knobs.kill_all();
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
        self.knobs.kill_all();
    }
}

pub async fn start() -> Server {
    start_with(identity(), Knobs::new(), "127.0.0.1:0".parse().unwrap()).await
}

pub async fn start_with(identity: Identity, knobs: Arc<Knobs>, addr: SocketAddr) -> Server {
    let listener = TcpListener::bind(addr).await.unwrap();
    let addr = listener.local_addr().unwrap();
    let acceptor = TlsAcceptor::from(identity.cfg.clone());
    let (k, spki) = (knobs.clone(), identity.spki);
    let task = tokio::spawn(async move {
        let mut idx = 0u64;
        loop {
            let Ok((tcp, _)) = listener.accept().await else { break };
            idx += 1;
            k.accepts.fetch_add(1, Ordering::SeqCst);
            let (acc, k2) = (acceptor.clone(), k.clone());
            tokio::spawn(async move {
                if let Ok(tls) = acc.accept(tcp).await {
                    k2.tls_ok.fetch_add(1, Ordering::SeqCst);
                    serve_conn(tls, k2, idx, spki).await;
                }
            });
        }
    });
    Server { addr, identity, knobs, task }
}

// ------------------------------------------------------------ credit tracking
#[derive(Default)]
struct SendCredit {
    conn: Mutex<i64>,
    streams: Mutex<HashMap<u32, i64>>,
}

impl SendCredit {
    fn try_take(&self, sid: u32, n: i64) -> bool {
        let mut c = self.conn.lock().unwrap();
        let mut s = self.streams.lock().unwrap();
        let sc = s.entry(sid).or_insert(0);
        if *c >= n && *sc >= n {
            *c -= n;
            *sc -= n;
            true
        } else {
            false
        }
    }
}

/// What the server still allows the client to send (decreases on arrival, grows when the
/// server's handler "consumes" and sends WINDOW).
#[derive(Default)]
struct RecvCredit {
    conn: Mutex<i64>,
    streams: Mutex<HashMap<u32, i64>>,
}

struct Inb {
    kind: u8,
    payload: Vec<u8>,
}

#[derive(Clone)]
struct Ctx {
    sid: u32,
    open: OpenRec,
    k: Arc<Knobs>,
    out: mpsc::UnboundedSender<Vec<u8>>,
    credit: Arc<SendCredit>,
    recv: Arc<RecvCredit>,
    max_frame: usize,
}

impl Ctx {
    fn send(&self, t: u8, payload: &[u8]) {
        if !self.k.mute.load(Ordering::SeqCst) {
            let _ = self.out.send(frame(t, self.sid, payload));
        }
    }
    async fn send_flow(&self, t: u8, payload: &[u8]) {
        while !self.credit.try_take(self.sid, payload.len() as i64) {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        self.send(t, payload);
    }
    async fn msg(&self, v: &Value) {
        self.send_flow(0x11, &cbor::encode(v).unwrap()).await;
    }
    fn end(&self, result: Value) {
        self.send(0x13, &cbor::encode(&m(vec![("result", result)])).unwrap());
    }
    fn error(&self, code: &str, status: u16, message: &str, retryable: bool, detail: Option<Value>) {
        let mut items = vec![
            ("code", Value::text(code)),
            ("status", Value::Int(status as i64)),
            ("message", Value::text(message)),
            ("retryable", Value::Bool(retryable)),
        ];
        if let Some(d) = detail {
            items.push(("detail", d));
        }
        self.send(0x14, &cbor::encode(&m(items)).unwrap());
    }
    /// Tells the client we consumed `n` octets of its upload.
    fn consumed(&self, n: usize) {
        if n == 0 {
            return;
        }
        let w = cbor::encode(&m(vec![("inc", Value::Int(n as i64))])).unwrap();
        *self.recv.conn.lock().unwrap() += n as i64;
        *self.recv.streams.lock().unwrap().entry(self.sid).or_insert(0) += n as i64;
        let _ = self.out.send(frame(0x16, self.sid, &w));
        let _ = self.out.send(frame(0x16, 0, &w));
    }
}

// ----------------------------------------------------------- connection loop
async fn serve_conn(tls: tokio_rustls::server::TlsStream<TcpStream>, k: Arc<Knobs>, idx: u64, spki: [u8; 32]) {
    let (mut rd, mut wr) = tokio::io::split(tls);
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let writer = tokio::spawn(async move {
        while let Some(b) = out_rx.recv().await {
            if wr.write_all(&b).await.is_err() || wr.flush().await.is_err() {
                break;
            }
        }
        let _ = wr.shutdown().await;
    });
    let counted = Arc::new(AtomicBool::new(false));
    let note_frame = |k: &Arc<Knobs>| {
        k.app_frames.fetch_add(1, Ordering::SeqCst);
        if !counted.swap(true, Ordering::SeqCst) {
            k.conns_with_app_frames.fetch_add(1, Ordering::SeqCst);
        }
    };

    // ---- handshake -------------------------------------------------------
    let Some((t, _, p)) = read_frame(&mut rd).await else { return };
    note_frame(&k);
    assert_eq!(t, 0x01, "first frame must be HELLO");
    assert!(p.len() <= 4096, "client HELLO exceeds the pre-auth limit");
    let hello = cbor::decode(&p).expect("HELLO is valid SNP-CBOR");
    let versions: Vec<u64> = hello.get("versions").unwrap().as_array().unwrap().iter().filter_map(|v| v.as_u64()).collect();
    assert!(versions.contains(&1));
    let client_max_frame = hello.get("max_frame").and_then(|v| v.as_u64()).unwrap();
    assert!(hello.get("client").and_then(|v| v.as_str()).is_some());
    if k.unsupported_version.load(Ordering::SeqCst) {
        let g = m(vec![("code", Value::text("unsupported_version")), ("message", Value::text("no")), ("supported", Value::Array(vec![Value::Int(2), Value::Int(3)]))]);
        let _ = out_tx.send(frame(0x07, 0, &cbor::encode(&g).unwrap()));
        drop(out_tx);
        let _ = writer.await;
        return;
    }
    let nonce: [u8; 32] = rand::random();
    let mut lim = k.limits.lock().unwrap().clone();
    if k.max_frame_override_dishonest.load(Ordering::SeqCst) {
        lim.max_frame = (client_max_frame as u32).saturating_add(1).max(lim.max_frame);
    }
    lim.max_frame = lim.max_frame.min(client_max_frame as u32);
    let features = k.features.lock().unwrap().clone();
    let mut limits = vec![
        ("max_frame", Value::Int(lim.max_frame as i64)),
        ("initial_window", Value::Int(lim.initial_window as i64)),
        ("conn_window", Value::Int(lim.conn_window as i64)),
        ("max_streams", Value::Int(lim.max_streams as i64)),
        ("ping_interval_ms", Value::Int(lim.ping_interval_ms as i64)),
        ("idle_timeout_ms", Value::Int(lim.idle_timeout_ms as i64)),
        ("idem_ttl_s", Value::Int(lim.idem_ttl_s as i64)),
    ];
    if k.out_of_range_limits.load(Ordering::SeqCst) {
        limits[3] = ("max_streams", Value::Int(5000));
    }
    let mut ok = vec![
        ("version", Value::Int(1)),
        ("server", Value::text("fake-snp/1.0")),
        ("nonce", Value::Bytes(nonce.to_vec())),
        ("features", Value::Array(features.iter().map(|f| Value::text(f.clone())).collect())),
        ("auth", Value::Array(vec![Value::text("hmac-sha256")])),
        ("limits", m(limits)),
    ];
    if k.missing_nonce.load(Ordering::SeqCst) {
        ok.retain(|(n, _)| *n != "nonce");
    }
    if k.garbage_hello_ok.load(Ordering::SeqCst) {
        let _ = out_tx.send(b"NOPE\x00\x00\x00\x00\x00\x00\x00\x00junk".to_vec());
        tokio::time::sleep(Duration::from_millis(200)).await;
        return;
    }
    if k.oversize_hello_ok.load(Ordering::SeqCst) {
        // 5000 > the 4096-octet pre-auth limit; the client must refuse it from the header alone.
        let _ = out_tx.send(frame(0x02, 0, &vec![0u8; 5000]));
        tokio::time::sleep(Duration::from_millis(200)).await;
        return;
    }
    let _ = out_tx.send(frame(0x02, 0, &cbor::encode(&m(ok)).unwrap()));

    let Some((t, _, p)) = read_frame(&mut rd).await else { return };
    note_frame(&k);
    assert_eq!(t, 0x03, "second frame must be AUTH");
    let auth = cbor::decode(&p).unwrap();
    let cnonce = auth.get("cnonce").unwrap().as_bytes().unwrap().to_vec();
    assert_eq!(cnonce.len(), 16);
    let proof = auth.get("proof").unwrap().as_bytes().unwrap().to_vec();
    let key = k_of(TOKEN);
    let mut mac = HmacSha256::new_from_slice(&key).unwrap();
    for part in [&b"SNP/1 client"[..], &nonce, &cnonce, &spki] {
        mac.update(part);
    }
    if k.reject_auth.load(Ordering::SeqCst) || mac.verify_slice(&proof).is_err() {
        let g = m(vec![("code", Value::text("unauthenticated")), ("message", Value::text("bad credentials"))]);
        let _ = out_tx.send(frame(0x07, 0, &cbor::encode(&g).unwrap()));
        drop(out_tx);
        let _ = writer.await;
        return;
    }
    let mut mac = HmacSha256::new_from_slice(&key).unwrap();
    for part in [&b"SNP/1 server"[..], &cnonce, &nonce, &spki] {
        mac.update(part);
    }
    let mut sp: Vec<u8> = mac.finalize().into_bytes().to_vec();
    if k.bad_server_proof.load(Ordering::SeqCst) {
        sp[0] ^= 0xff;
    }
    let ok2 = m(vec![("proof", Value::Bytes(sp)), ("session", Value::text(format!("sess-{idx}"))), ("api_version", Value::text("1.9"))]);
    let _ = out_tx.send(frame(0x04, 0, &cbor::encode(&ok2).unwrap()));

    // ---- main loop ---------------------------------------------------------
    let credit = Arc::new(SendCredit::default());
    *credit.conn.lock().unwrap() = lim.conn_window as i64;
    let mut inbound: HashMap<u32, mpsc::UnboundedSender<Inb>> = HashMap::new();
    let mut last_id = 0u32;
    let recv = Arc::new(RecvCredit::default());
    *recv.conn.lock().unwrap() = lim.conn_window as i64;
    let mut kill = k.kill.subscribe();
    loop {
        let fr = tokio::select! {
            f = read_frame(&mut rd) => f,
            _ = kill.changed() => { return; }
        };
        let Some((t, sid, p)) = fr else {
            // Connection ended without END/CANCEL: every open stream is cancelled.
            for (_, tx) in inbound.drain() {
                let _ = tx.send(Inb { kind: 0xff, payload: vec![] });
            }
            return;
        };
        note_frame(&k);
        match t {
            0x10 => {
                if sid % 2 == 0 || sid <= last_id {
                    k.order_violation.store(true, Ordering::SeqCst);
                }
                last_id = sid;
                let v = cbor::decode(&p).expect("OPEN is valid SNP-CBOR");
                let rec = OpenRec {
                    conn: idx,
                    stream: sid,
                    op: v.get("op").unwrap().as_str().unwrap().to_string(),
                    args: v.get("args").cloned().unwrap_or(Value::Map(vec![])),
                    idem: v.get("idem").and_then(|x| x.as_str()).map(String::from),
                    deadline_ms: v.get("deadline_ms").and_then(|x| x.as_u64()),
                };
                k.opens.lock().unwrap().push(rec.clone());
                if matches!(rec.op.as_str(), "mutate" | "get_thing" | "later" | "other") {
                    if k.drop_next.load(Ordering::SeqCst) > 0 {
                        k.drop_next.fetch_sub(1, Ordering::SeqCst);
                        return; // abrupt close, no reply
                    }
                    if rec.op == "later" && k.goaway_next.load(Ordering::SeqCst) > 0 {
                        k.goaway_next.fetch_sub(1, Ordering::SeqCst);
                        let g = m(vec![("code", Value::text("shutdown")), ("message", Value::text("restarting")), ("last_stream_id", Value::Int((sid as i64 - 2).max(0)))]);
                        let _ = out_tx.send(frame(0x07, 0, &cbor::encode(&g).unwrap()));
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        return;
                    }
                }
                credit.streams.lock().unwrap().insert(sid, lim.initial_window as i64);
                recv.streams.lock().unwrap().insert(sid, lim.initial_window as i64);
                let (tx, rx) = mpsc::unbounded_channel();
                inbound.insert(sid, tx);
                let ctx = Ctx { sid, open: rec, k: k.clone(), out: out_tx.clone(), credit: credit.clone(), recv: recv.clone(), max_frame: lim.max_frame as usize };
                tokio::spawn(handle(ctx, rx));
            }
            0x11 | 0x12 | 0x13 | 0x15 => {
                if t == 0x12 || t == 0x11 {
                    let mut c = recv.conn.lock().unwrap();
                    *c -= p.len() as i64;
                    let mut ss = recv.streams.lock().unwrap();
                    let sc = ss.entry(sid).or_insert(0);
                    *sc -= p.len() as i64;
                    if *c < 0 || *sc < 0 {
                        k.client_overran_credit.store(true, Ordering::SeqCst);
                    }
                    if p.len() > lim.max_frame as usize {
                        k.client_overran_credit.store(true, Ordering::SeqCst);
                    }
                }
                if t == 0x15 {
                    k.cancels.lock().unwrap().push(sid);
                }
                if let Some(tx) = inbound.get(&sid) {
                    let _ = tx.send(Inb { kind: t, payload: p });
                }
            }
            0x16 => {
                let v = cbor::decode(&p).unwrap();
                let inc = v.get("inc").unwrap().as_u64().unwrap() as i64;
                if sid == 0 {
                    *credit.conn.lock().unwrap() += inc;
                } else if let Some(c) = credit.streams.lock().unwrap().get_mut(&sid) {
                    *c += inc;
                }
            }
            0x05 => {
                k.pings_rx.fetch_add(1, Ordering::SeqCst);
                if !k.mute.load(Ordering::SeqCst) {
                    let _ = out_tx.send(frame(0x06, 0, &p));
                }
            }
            0x06 => k.pongs_rx.lock().unwrap().push(p),
            0x07 => {
                let v = cbor::decode(&p).unwrap_or(Value::Null);
                k.goaways_rx.lock().unwrap().push(v.get("code").and_then(|c| c.as_str()).unwrap_or("").to_string());
                return;
            }
            other => panic!("unexpected frame type 0x{other:02x} from client"),
        }
    }
}

// ---------------------------------------------------------------- op handlers
async fn handle(ctx: Ctx, mut inb: mpsc::UnboundedReceiver<Inb>) {
    let args = ctx.open.args.clone();
    let arg_u = |k: &str, d: u64| args.get(k).and_then(|v| v.as_u64()).unwrap_or(d);
    match ctx.open.op.as_str() {
        // Scripted ops, the test ops, and every operation of the real catalogue behave as `echo`
        // unless a script says otherwise.
        op if matches!(op, "echo" | "mutate" | "get_thing" | "later" | "other")
            || (crate::snp::adapter::table().by_name(op).is_some()
                && !matches!(op, "file.get" | "file.upload" | "event.subscribe" | "chat.generate" | "agent.completion")) =>
        {
            let script = ctx.k.scripts.lock().unwrap().get(&ctx.open.op).cloned();
            if let Some(Script { replies }) = script {
                run_script(&ctx, replies, &mut inb).await;
                return;
            }
            ctx.end(m(vec![("op", Value::text(ctx.open.op.clone())), ("args", args.clone()), ("idem", ctx.open.idem.clone().map(Value::Text).unwrap_or(Value::Null))]));
        }
        "count" => {
            for i in 0..arg_u("n", 3) {
                ctx.msg(&m(vec![("i", Value::Int(i as i64))])).await;
            }
            ctx.end(Value::Null);
        }
        "hang" => {
            // Terminates on CANCEL, or on the deadline when the client sent one.
            let deadline = ctx.open.deadline_ms.map(Duration::from_millis);
            let wait = async {
                while let Some(i) = inb.recv().await {
                    if i.kind == 0x15 || i.kind == 0xff {
                        return true;
                    }
                }
                true
            };
            match deadline {
                Some(d) => {
                    if tokio::time::timeout(d, wait).await.is_err() {
                        ctx.error("deadline_exceeded", 504, "deadline exceeded", true, None);
                    } else {
                        ctx.error("cancelled", 499, "cancelled", false, None);
                    }
                }
                None => {
                    wait.await;
                    ctx.error("cancelled", 499, "cancelled", false, None);
                }
            }
        }
        "blob" => {
            let size = arg_u("size", 0) as usize;
            let chunk = (arg_u("chunk", 16384) as usize).min(ctx.max_frame);
            ctx.msg(&m(vec![("meta", m(vec![("mime", Value::text("application/octet-stream")), ("size", Value::Int(size as i64)), ("start", Value::Int(0)), ("end", Value::Int(size as i64 - 1))]))])).await;
            let mut sent = 0usize;
            while sent < size {
                let n = chunk.min(size - sent);
                let data: Vec<u8> = (sent..sent + n).map(|i| (i % 251) as u8).collect();
                ctx.send_flow(0x12, &data).await;
                sent += n;
                ctx.k.blob_sent.store(sent as u64, Ordering::SeqCst);
            }
            ctx.k.blob_done.store(true, Ordering::SeqCst);
            ctx.end(Value::Null);
        }
        "file.get" => {
            let fid = args.get("fid").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if fid == "f_missing" {
                ctx.error("not_found", 404, "file not found", false, None);
                return;
            }
            let body: Vec<u8> = (0..300u32).map(|i| (i % 200) as u8).collect();
            let (start, end) = match args.get("range") {
                Some(r) => (r.get("start").and_then(|v| v.as_u64()).unwrap_or(0) as usize, r.get("end").and_then(|v| v.as_u64()).map(|e| e as usize).unwrap_or(body.len() - 1)),
                None => (0, body.len() - 1),
            };
            ctx.msg(&m(vec![("meta", m(vec![("mime", Value::text("image/png")), ("size", Value::Int(body.len() as i64)), ("start", Value::Int(start as i64)), ("end", Value::Int(end as i64))]))])).await;
            for c in body[start..=end].chunks(128) {
                ctx.send_flow(0x12, c).await;
            }
            ctx.end(Value::Null);
        }
        "file.upload" => {
            let mut total = 0u64;
            loop {
                let Some(i) = inb.recv().await else {
                    ctx.k.upload_eof.store(true, Ordering::SeqCst);
                    break;
                };
                match i.kind {
                    0x12 => {
                        total += i.payload.len() as u64;
                        ctx.k.upload_bytes.store(total, Ordering::SeqCst);
                        let d = ctx.k.sink_delay_ms.load(Ordering::SeqCst);
                        if d > 0 {
                            tokio::time::sleep(Duration::from_millis(d)).await;
                        }
                        // Replenish only after "processing", like a well-behaved receiver.
                        ctx.consumed(i.payload.len());
                    }
                    0x13 => {
                        ctx.k.upload_done.store(true, Ordering::SeqCst);
                        ctx.end(m(vec![("id", Value::text("up_1")), ("size", Value::Int(total as i64)), ("name", args.get("name").cloned().unwrap_or(Value::Null)), ("mime", args.get("mime").cloned().unwrap_or(Value::Null))]));
                        return;
                    }
                    0x15 => {
                        ctx.k.upload_cancelled.store(true, Ordering::SeqCst);
                        ctx.error("cancelled", 499, "cancelled", false, None);
                        return;
                    }
                    0xff => {
                        ctx.k.upload_eof.store(true, Ordering::SeqCst);
                        return;
                    }
                    _ => {}
                }
                let limit = ctx.open.args.get("limit").and_then(|v| v.as_u64()).unwrap_or_else(|| ctx.k.upload_limit.load(Ordering::SeqCst));
                if limit > 0 && total > limit {
                    ctx.error("too_large", 413, "upload too large", false, None);
                    return;
                }
            }
        }
        "event.subscribe" => {
            let mut events = ctx.k.events.subscribe();
            ctx.msg(&m(vec![("type", Value::text("hello")), ("data", m(vec![("api_version", Value::text("1.9"))]))])).await;
            loop {
                tokio::select! {
                    ev = events.recv() => {
                        if let Ok(v) = ev {
                            ctx.msg(&v).await;
                        }
                    },
                    i = inb.recv() => match i {
                        Some(i) if i.kind == 0x11 => {
                            ctx.consumed(i.payload.len());
                            ctx.k.ctrl_msgs.lock().unwrap().push(cbor::decode(&i.payload).unwrap());
                        }
                        Some(i) if i.kind == 0x15 => { ctx.error("cancelled", 499, "cancelled", false, None); return; }
                        Some(_) => {}
                        None => return,
                    },
                }
            }
        }
        "ping_me" => {
            let _ = ctx.out.send(frame(0x05, 0, &[9, 8, 7, 6, 5, 4, 3, 2]));
            ctx.end(Value::Null);
        }
        "raw" => {
            // Writes attacker-chosen octets straight onto the connection.
            if let Some(b) = args.get("bytes").and_then(|v| v.as_bytes()) {
                let _ = ctx.out.send(b.to_vec());
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        "malformed_msg" => {
            ctx.send(0x11, &[0x1c]); // reserved additional information: not valid SNP-CBOR
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        "stall_then_error" => {
            ctx.error("stalled", 504, "stalled", true, None);
        }
        "chat.generate" | "agent.completion" => {
            let script = ctx.k.scripts.lock().unwrap().get(&ctx.open.op).cloned();
            if let Some(Script { replies }) = script {
                run_script(&ctx, replies, &mut inb).await;
            } else {
                ctx.error("unknown_op", 404, "no script", false, None);
            }
        }
        other => ctx.error("unknown_op", 404, &format!("unknown operation {other}"), false, None),
    }
}

async fn run_script(ctx: &Ctx, replies: Vec<Reply>, inb: &mut mpsc::UnboundedReceiver<Inb>) {
    for r in replies {
        match r {
            Reply::Msg(v) => ctx.msg(&v).await,
            Reply::End(v) => ctx.end(v),
            Reply::Error { code, status, message, retryable, detail } => ctx.error(&code, status, &message, retryable, detail),
            Reply::WaitCancel => {
                while let Some(i) = inb.recv().await {
                    if i.kind == 0x15 || i.kind == 0xff {
                        break;
                    }
                }
                ctx.error("cancelled", 499, "cancelled", false, None);
            }
        }
    }
}
