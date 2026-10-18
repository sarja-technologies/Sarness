//! Loopback adapter: translates the HTTP/JSON requests of the webview (which keeps
//! talking to the authenticated loopback proxy) into SNP operations.
//!
//! When the active profile uses the SNP transport the proxy never contacts the
//! engine over HTTP at all; every request is mapped through the operation
//! catalogue (`engine/sarness_engine/snp/ops.json`), answered in the JSON shape the
//! HTTP API would have produced, and errors come back as `{"detail": …}` with the
//! numeric status of the SNP `ERROR` frame.

use super::cbor::{self, Value};
use super::client::{CallOpts, Event, Retry, SnpClient, SnpStream, SubEvent};
use super::error::SnpError;
use crate::proxy;
use axum::{
    body::Body,
    extract::{
        ws::{Message as WsMsg, WebSocket},
        FromRequest, Multipart, Request, WebSocketUpgrade,
    },
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use bytes::Bytes;
use futures_util::{SinkExt, Stream, StreamExt};
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::io::AsyncWriteExt;

const OPS_JSON: &str = include_str!("../../../../engine/sarness_engine/snp/ops.json");
const MAX_JSON_BODY: usize = 64 * 1024 * 1024;
const HEARTBEAT: Duration = Duration::from_secs(15);

/// Operations that may legitimately run longer than the server's default unary
/// deadline (§6.5: the catalogue carries no "long-running" marker, so the client
/// states an explicit generous deadline for these).
const LONG_RUNNING: &[&str] = &[
    "paintbrush.render", "paintbrush.warmup", "paintbrush.install", "paintbrush.render.export", "paintbrush.render.save",
    "paintbrush.mask.export", "llm.load", "llm.unload", "model.install", "model.rescan", "runtime.llama.install",
    "gateway.refresh", "llm.endpoint.refresh", "worker.release", "worker.stop", "paintbrush.release",
    "generation.bulk", "chat.conversation.import", "hf.search", "hf.inspect",
];
const LONG_DEADLINE: Duration = Duration::from_secs(3600);

/// What the proxy holds while an SNP profile is active.
pub struct SnpBackend {
    pub client: SnpClient,
    pub profile_id: String,
}

// ---------------------------------------------------------------- catalogue
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpMode {
    Unary,
    Stream,
    Download,
    Upload,
    Subscribe,
}

#[derive(Debug, Clone)]
enum Seg {
    Lit(String),
    Param(String),
    /// `{name:path}` - matches the rest of the path.
    PathParam(String),
}

#[derive(Debug, Clone)]
pub struct OpDef {
    pub op: String,
    pub method: String,
    pub route: String,
    pub mode: OpMode,
    pub params: Vec<String>,
    segs: Vec<Seg>,
    /// Request-body JSON goes under this argument name (`body`, or `patch` for settings.patch).
    pub body_key: Option<&'static str>,
}

impl OpDef {
    pub fn path_param_names(&self) -> Vec<&str> {
        self.segs
            .iter()
            .filter_map(|s| match s {
                Seg::Param(n) | Seg::PathParam(n) => Some(n.as_str()),
                Seg::Lit(_) => None,
            })
            .collect()
    }
}

pub struct OpTable {
    pub ops: Vec<OpDef>,
}

pub struct Routed<'a> {
    pub op: &'a OpDef,
    pub path_params: Vec<(String, String)>,
}

fn parse_template(route: &str) -> Result<Vec<Seg>, String> {
    if !route.starts_with('/') {
        return Err(format!("route {route:?} must start with /"));
    }
    route
        .trim_matches('/')
        .split('/')
        .map(|seg| {
            if let Some(inner) = seg.strip_prefix('{') {
                let inner = inner.strip_suffix('}').ok_or_else(|| format!("unterminated parameter in {route:?}"))?;
                match inner.split_once(':') {
                    Some((n, "path")) if !n.is_empty() => Ok(Seg::PathParam(n.to_string())),
                    Some(_) => Err(format!("unsupported converter in {route:?}")),
                    None if !inner.is_empty() => Ok(Seg::Param(inner.to_string())),
                    None => Err(format!("empty parameter name in {route:?}")),
                }
            } else if seg.is_empty() || seg.contains(['{', '}']) {
                Err(format!("bad path segment in {route:?}"))
            } else {
                Ok(Seg::Lit(seg.to_string()))
            }
        })
        .collect()
}

impl OpTable {
    pub fn parse(json: &str) -> Result<OpTable, String> {
        let v: serde_json::Value = serde_json::from_str(json).map_err(|e| format!("ops.json: {e}"))?;
        if v.get("protocol").and_then(|p| p.as_str()) != Some("SNP/1") {
            return Err("ops.json: protocol must be SNP/1".into());
        }
        let mut ops = Vec::new();
        for o in v.get("ops").and_then(|o| o.as_array()).ok_or("ops.json: missing ops")? {
            let s = |k: &str| o.get(k).and_then(|x| x.as_str()).map(String::from).ok_or(format!("ops.json: op without {k}"));
            let op = s("op")?;
            let route = s("route")?;
            let mode = match s("mode")?.as_str() {
                "unary" => OpMode::Unary,
                "stream" => OpMode::Stream,
                "download" => OpMode::Download,
                "upload" => OpMode::Upload,
                "subscribe" => OpMode::Subscribe,
                m => return Err(format!("ops.json: {op}: unknown mode {m}")),
            };
            let params: Vec<String> = o
                .get("params")
                .and_then(|p| p.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                .unwrap_or_default();
            let body_key = if params.iter().any(|p| p == "body") {
                Some("body")
            } else if params.iter().any(|p| p == "patch") {
                Some("patch")
            } else {
                None
            };
            ops.push(OpDef { op, method: s("method")?.to_uppercase(), segs: parse_template(&route).map_err(|e| format!("ops.json: {e}"))?, route, mode, params, body_key });
        }
        Ok(OpTable { ops })
    }

    pub fn by_name(&self, name: &str) -> Option<&OpDef> {
        self.ops.iter().find(|o| o.op == name)
    }

    /// Finds the operation for an HTTP request; literal segments beat parameters.
    pub fn route(&self, method: &str, path: &str) -> Option<Routed<'_>> {
        let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
        let mut best: Option<(usize, Routed<'_>)> = None;
        for op in self.ops.iter().filter(|o| o.method.eq_ignore_ascii_case(method)) {
            if let Some(pp) = match_segs(&op.segs, &parts) {
                let score = op.segs.iter().filter(|s| matches!(s, Seg::Lit(_))).count();
                if best.as_ref().map(|(s, _)| score > *s).unwrap_or(true) {
                    best = Some((score, Routed { op, path_params: pp }));
                }
            }
        }
        best.map(|(_, r)| r)
    }
}

fn decode_seg(s: &str) -> String {
    percent_encoding::percent_decode_str(s).decode_utf8_lossy().into_owned()
}

fn match_segs(segs: &[Seg], parts: &[&str]) -> Option<Vec<(String, String)>> {
    let mut out = Vec::new();
    let mut i = 0;
    for seg in segs {
        match seg {
            Seg::Lit(l) => {
                if parts.get(i) == Some(&l.as_str()) {
                    i += 1;
                } else {
                    return None;
                }
            }
            Seg::Param(n) => {
                let p = parts.get(i).filter(|p| !p.is_empty())?;
                out.push((n.clone(), decode_seg(p)));
                i += 1;
            }
            Seg::PathParam(n) => {
                if i >= parts.len() {
                    return None;
                }
                out.push((n.clone(), decode_seg(&parts[i..].join("/"))));
                i = parts.len();
            }
        }
    }
    (i == parts.len()).then_some(out)
}

pub fn table() -> &'static OpTable {
    static T: OnceLock<OpTable> = OnceLock::new();
    T.get_or_init(|| OpTable::parse(OPS_JSON).expect("embedded SNP operation catalogue is valid"))
}

// -------------------------------------------------------------- args building
/// Query strings are text; coerce the few parameters the engine declares as numbers/booleans.
fn coerce_query(key: &str, val: &str) -> Value {
    match key {
        "limit" => val.parse::<i64>().map(Value::Int).unwrap_or_else(|_| Value::text(val)),
        "since" | "until" | "before" => val.parse::<f64>().ok().filter(|f| f.is_finite()).map(Value::Float).unwrap_or_else(|| Value::text(val)),
        "presets" | "favorite" | "trash" | "permanent" => match val.to_ascii_lowercase().as_str() {
            "true" | "1" | "yes" | "on" => Value::Bool(true),
            "false" | "0" | "no" | "off" => Value::Bool(false),
            _ => Value::text(val),
        },
        _ => Value::text(val),
    }
}

/// Builds the SNP `args` map: path parameters, then declared query parameters, then
/// the JSON request body under `body` (or `patch`).
pub fn build_args(r: &Routed<'_>, query: Option<&str>, body: Option<&serde_json::Value>) -> Value {
    let mut args: Vec<(String, Value)> = r.path_params.iter().map(|(k, v)| (k.clone(), Value::Text(v.clone()))).collect();
    if let Some(q) = query {
        for (k, v) in url::form_urlencoded::parse(q.as_bytes()) {
            if r.op.params.iter().any(|p| p == k.as_ref()) && Some(k.as_ref()) != r.op.body_key && !args.iter().any(|(a, _)| a == k.as_ref()) {
                let val = coerce_query(&k, &v);
                args.push((k.into_owned(), val));
            }
        }
    }
    if let (Some(key), Some(b)) = (r.op.body_key, body) {
        args.push((key.to_string(), cbor::from_json(b)));
    }
    Value::Map(args)
}

// --------------------------------------------------------------- HTTP replies
fn finish(mut r: Response) -> Response {
    proxy::cors(r.headers_mut());
    r
}

fn json_response(status: StatusCode, body: &serde_json::Value) -> Response {
    finish((status, [(header::CONTENT_TYPE, "application/json")], serde_json::to_vec(body).unwrap_or_default()).into_response())
}

/// `{detail: …}` body for an error. Structured `detail` maps from the server are merged
/// in (the HTTP API returns such bodies, e.g. `{detail, superseded: true}`).
pub fn error_body(e: &SnpError) -> serde_json::Value {
    match e {
        SnpError::Remote(r) => {
            let mut obj = serde_json::Map::new();
            obj.insert("detail".into(), serde_json::Value::String(r.message.clone()));
            obj.insert("code".into(), serde_json::Value::String(r.code.clone()));
            match &r.detail {
                Some(Value::Map(m)) => {
                    for (k, v) in m {
                        obj.insert(k.clone(), cbor::to_json(v));
                    }
                }
                Some(arr @ Value::Array(_)) => {
                    obj.insert("detail".into(), cbor::to_json(arr));
                }
                _ => {}
            }
            serde_json::Value::Object(obj)
        }
        other => serde_json::json!({ "detail": other.to_string(), "error": other.kind() }),
    }
}

pub fn error_response(e: &SnpError) -> Response {
    let status = StatusCode::from_u16(e.http_status()).ok().filter(|s| s.as_u16() >= 400).unwrap_or(StatusCode::BAD_GATEWAY);
    json_response(status, &error_body(e))
}

fn bad_request(msg: &str) -> Response {
    json_response(StatusCode::BAD_REQUEST, &serde_json::json!({ "detail": msg }))
}

pub fn sse_event(v: &Value) -> Bytes {
    Bytes::from(format!("data: {}\n\n", serde_json::to_string(&cbor::to_json(v)).unwrap_or_else(|_| "{}".into())))
}

// ------------------------------------------------------------------- dispatch
pub async fn handle(backend: &Arc<SnpBackend>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    let method = parts.method.as_str().to_string();
    let path = parts.uri.path().to_string();
    let query = parts.uri.query().map(String::from);
    let tbl = table();

    if method == "POST" && path.trim_end_matches('/') == "/v1/uploads" {
        return upload(backend, Request::from_parts(parts, body)).await;
    }
    let Some(routed) = tbl.route(&method, &path) else {
        return json_response(StatusCode::NOT_FOUND, &serde_json::json!({ "detail": format!("{method} {path} is not available over SNP") }));
    };
    let op = routed.op;
    match op.mode {
        OpMode::Subscribe => bad_request("the event stream requires a WebSocket connection"),
        OpMode::Download => bad_request("invalid file id"),
        OpMode::Upload => bad_request("uploads must be sent as multipart/form-data"),
        OpMode::Unary | OpMode::Stream => {
            let json = match read_json(body).await {
                Ok(j) => j,
                Err(r) => return r,
            };
            let args = build_args(&routed, query.as_deref(), json.as_ref());
            if op.mode == OpMode::Stream {
                sse(backend, &op.op, args).await
            } else {
                unary(backend, op, args).await
            }
        }
    }
}

async fn read_json(body: Body) -> Result<Option<serde_json::Value>, Response> {
    let bytes = axum::body::to_bytes(body, MAX_JSON_BODY).await.map_err(|_| json_response(StatusCode::PAYLOAD_TOO_LARGE, &serde_json::json!({ "detail": "request body is too large" })))?;
    if bytes.iter().all(|b| b.is_ascii_whitespace()) {
        return Ok(None);
    }
    serde_json::from_slice(&bytes).map(Some).map_err(|_| bad_request("the request body is not valid JSON"))
}

async fn unary(backend: &Arc<SnpBackend>, op: &OpDef, args: Value) -> Response {
    let mut opts = CallOpts::new(if op.method == "GET" { Retry::Safe } else { Retry::Idem });
    if LONG_RUNNING.contains(&op.op.as_str()) {
        opts.deadline = Some(LONG_DEADLINE);
    }
    match backend.client.call(&op.op, args, opts).await {
        Ok(v) => {
            if op.op == "chat.conversation.export" {
                if let Value::Text(t) = &v {
                    return finish(([(header::CONTENT_TYPE, "text/markdown; charset=utf-8")], t.clone()).into_response());
                }
            }
            json_response(StatusCode::OK, &cbor::to_json(&v))
        }
        Err(e) => error_response(&e),
    }
}

// ------------------------------------------------------------------------ SSE
struct SseState {
    stream: Option<SnpStream>,
    first: Option<Value>,
}

/// Converts MSG events to the `data: {json}\n\n` bytes the webview already parses.
pub fn sse_stream(stream: SnpStream, first: Option<Value>) -> impl Stream<Item = Result<Bytes, std::io::Error>> + Send + 'static {
    futures_util::stream::unfold(SseState { stream: Some(stream), first }, |mut st| async move {
        if let Some(v) = st.first.take() {
            return Some((Ok(sse_event(&v)), st));
        }
        loop {
            let s = st.stream.as_mut()?;
            match s.recv().await {
                Ok(Event::Msg(v)) => return Some((Ok(sse_event(&v)), st)),
                Ok(Event::Data(_)) => continue,
                Ok(Event::End(_)) => return None,
                Err(SnpError::Remote(r)) => {
                    // Failure after the stream started: the HTTP engine reports it in-band, too.
                    st.stream = None;
                    let ev = Value::map([("type", Value::text("error")), ("message", Value::text(r.message))]);
                    return Some((Ok(sse_event(&ev)), st));
                }
                Err(e) => {
                    st.stream = None;
                    return Some((Err(std::io::Error::other(e.to_string())), st));
                }
            }
        }
    })
}

async fn sse(backend: &Arc<SnpBackend>, op: &str, args: Value) -> Response {
    let mut stream = match backend.client.open(op, args).await {
        Ok(s) => s,
        Err(e) => return error_response(&e),
    };
    // Wait for the first event so validation failures become ordinary HTTP errors, as over HTTP.
    let first = loop {
        match stream.recv().await {
            Ok(Event::Msg(v)) => break Some(v),
            Ok(Event::End(_)) => break None,
            Ok(Event::Data(_)) => continue,
            Err(e) => return error_response(&e),
        }
    };
    let mut r = Response::new(Body::from_stream(sse_stream(stream, first)));
    let h = r.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static("text/event-stream"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    h.insert("x-accel-buffering", HeaderValue::from_static("no"));
    finish(r)
}

// --------------------------------------------------------------------- upload
async fn upload(backend: &Arc<SnpBackend>, req: Request) -> Response {
    let mut mp = match Multipart::from_request(req, &()).await {
        Ok(m) => m,
        Err(e) => return bad_request(&format!("invalid upload: {e}")),
    };
    loop {
        let mut field = match mp.next_field().await {
            Ok(Some(f)) => f,
            Ok(None) => return bad_request("no file in the upload"),
            Err(e) => return bad_request(&format!("invalid upload: {e}")),
        };
        if field.file_name().is_none() && field.name() != Some("file") {
            continue;
        }
        let mut args = Vec::new();
        if let Some(n) = field.file_name() {
            args.push(("name".to_string(), Value::text(n)));
        }
        if let Some(m) = field.content_type() {
            args.push(("mime".to_string(), Value::text(m)));
        }
        let mut s = match backend.client.open("file.upload", Value::Map(args)).await {
            Ok(s) => s,
            Err(e) => return error_response(&e),
        };
        loop {
            match field.chunk().await {
                Ok(Some(chunk)) => {
                    // Flow-controlled: a slow server pushes back on the webview's upload.
                    if s.send_data(chunk).await.is_err() {
                        break; // the server ended the stream (e.g. too_large); its ERROR is queued
                    }
                }
                Ok(None) => {
                    let _ = s.end();
                    break;
                }
                // The webview went away mid-upload: dropping `s` sends CANCEL and the server discards the data.
                Err(e) => return bad_request(&format!("upload interrupted: {e}")),
            }
        }
        return loop {
            match s.recv().await {
                Ok(Event::End(v)) => break json_response(StatusCode::OK, &cbor::to_json(&v)),
                Ok(_) => continue,
                Err(e) => break error_response(&e),
            }
        };
    }
}

// ------------------------------------------------------------------ file.get
#[derive(Debug, Clone, PartialEq)]
pub struct FileMeta {
    pub mime: String,
    pub size: u64,
    pub start: u64,
    pub end: u64,
    pub name: Option<String>,
}

/// Downloads `fid` (optionally a byte range) into `dest`, verifying the received length.
pub async fn download_to_file(client: &SnpClient, fid: &str, dest: &Path, range: Option<(u64, Option<u64>)>) -> Result<FileMeta, SnpError> {
    let mut args = vec![("fid".to_string(), Value::text(fid))];
    if let Some((start, end)) = range {
        let mut r = vec![("start".to_string(), Value::Int(start as i64))];
        if let Some(e) = end {
            r.push(("end".to_string(), Value::Int(e as i64)));
        }
        args.push(("range".into(), Value::Map(r)));
    }
    let mut s = client.open("file.get", Value::Map(args)).await?;
    let meta = match s.recv().await? {
        Event::Msg(m) => {
            let m = m.get("meta").cloned().unwrap_or(m);
            let size = m.get("size").and_then(Value::as_u64).unwrap_or(0);
            let start = m.get("start").and_then(Value::as_u64).unwrap_or(0);
            FileMeta {
                mime: m.get("mime").and_then(Value::as_str).unwrap_or("application/octet-stream").to_string(),
                size,
                start,
                end: m.get("end").and_then(Value::as_u64).unwrap_or(size.saturating_sub(1)),
                name: m.get("name").and_then(Value::as_str).map(String::from),
            }
        }
        Event::End(_) => return Err(SnpError::Protocol("file.get ended without metadata".into())),
        Event::Data(_) => return Err(SnpError::Protocol("file.get sent data before metadata".into())),
    };
    let mut f = tokio::fs::File::create(dest).await.map_err(|e| SnpError::Io(e.to_string()))?;
    let mut got: u64 = 0;
    loop {
        match s.recv().await? {
            Event::Data(b) => {
                got += b.len() as u64;
                f.write_all(&b).await.map_err(|e| SnpError::Io(e.to_string()))?;
            }
            Event::Msg(_) => continue,
            Event::End(_) => break,
        }
    }
    f.flush().await.map_err(|e| SnpError::Io(e.to_string()))?;
    let expected = if meta.size == 0 { 0 } else { meta.end + 1 - meta.start };
    if got != expected {
        return Err(SnpError::Protocol(format!("file transfer was truncated ({got} of {expected} bytes)")));
    }
    Ok(meta)
}

// ------------------------------------------------------------- event stream
pub async fn ws(backend: Arc<SnpBackend>, upgrade: WebSocketUpgrade) -> Response {
    let sub = match backend.client.subscribe_events(vec![], false).await {
        Ok(s) => s,
        Err(e) => return error_response(&e),
    };
    let api_version = backend.client.info().await.map(|i| i.api_version.clone()).unwrap_or_default();
    upgrade.on_upgrade(move |sock| bridge(sock, sub, api_version))
}

pub fn hello_text(api_version: &str) -> String {
    serde_json::json!({ "type": "hello", "data": { "api_version": api_version, "heartbeat_s": HEARTBEAT.as_secs() } }).to_string()
}

/// Webview WebSocket ⇄ `event.subscribe`. The shape matches engine/api/ws.py: a hello,
/// engine events as `{type, ts, data}`, a heartbeat after 15 s of silence, and
/// `{"type":"pong"}` for `{"type":"ping"}`. When the SNP subscription is lost the
/// socket is closed so the webview marks the server offline and refreshes on reconnect.
async fn bridge(sock: WebSocket, mut sub: super::client::EventSubscription, api_version: String) {
    let (mut tx, mut rx) = sock.split();
    if tx.send(WsMsg::Text(hello_text(&api_version).into())).await.is_err() {
        return;
    }
    let hb = tokio::time::sleep(HEARTBEAT);
    tokio::pin!(hb);
    loop {
        tokio::select! {
            ev = sub.next() => match ev {
                SubEvent::Event(v) => {
                    if v.get("type").and_then(Value::as_str) == Some("hello") {
                        continue;
                    }
                    let text = serde_json::to_string(&cbor::to_json(&v)).unwrap_or_default();
                    if tx.send(WsMsg::Text(text.into())).await.is_err() {
                        break;
                    }
                    hb.as_mut().reset(tokio::time::Instant::now() + HEARTBEAT);
                }
                SubEvent::Resubscribed => {}
                SubEvent::Disconnected(_) | SubEvent::Fatal(_) => break,
            },
            _ = &mut hb => {
                if tx.send(WsMsg::Text("{\"type\":\"heartbeat\"}".into())).await.is_err() {
                    break;
                }
                hb.as_mut().reset(tokio::time::Instant::now() + HEARTBEAT);
            }
            m = rx.next() => match m {
                Some(Ok(WsMsg::Text(t))) => {
                    if t.as_str().contains("\"ping\"") && tx.send(WsMsg::Text("{\"type\":\"pong\"}".into())).await.is_err() {
                        break;
                    }
                }
                Some(Ok(WsMsg::Close(_))) | None | Some(Err(_)) => break,
                Some(Ok(_)) => {}
            },
        }
    }
    let _ = tx.send(WsMsg::Close(None)).await;
}

/// Name → op lookup used by tests and diagnostics.
pub fn op_names() -> HashMap<&'static str, OpMode> {
    table().ops.iter().map(|o| (o.op.as_str(), o.mode)).collect()
}
