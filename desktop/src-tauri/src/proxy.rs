//! Authenticated loopback reverse proxy between the webview and the engine.
//!
//! * The webview only ever talks to `http://127.0.0.1:<port>`; the engine token is
//!   injected here and never exposed to JavaScript.
//! * Every request must carry the per-launch session key (header
//!   `x-sarness-session` or query `s=`), so other local processes cannot use it.
//! * Streaming bodies (SSE chat) and the `/v1/events` WebSocket are forwarded.
//! * `/v1/files/<id>` responses are content-addressed and immutable, so they are
//!   cached on disk and served with Range support — even while offline.

use axum::{
    body::Body,
    extract::{ws::Message as AxMsg, Request, State, WebSocketUpgrade},
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
    Router,
};
use crate::snp::adapter::{self, SnpBackend};
use futures_util::{SinkExt, StreamExt, TryStreamExt};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime},
};
use tokio::sync::RwLock;
use tokio_tungstenite::tungstenite::{self, client::IntoClientRequest};
use tower::ServiceExt;

pub const CACHE_LIMIT_BYTES: u64 = 6 * 1024 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct Upstream {
    pub base: String,
    pub token: String,
}

pub struct ProxyShared {
    pub upstream: RwLock<Option<Upstream>>,
    /// Set instead of `upstream` while an SNP profile is active: the proxy then never
    /// speaks HTTP to the engine (see `snp::adapter`).
    pub snp: RwLock<Option<Arc<SnpBackend>>>,
    pub session: String,
    pub client: reqwest::Client,
    pub cache_dir: PathBuf,
}

impl ProxyShared {
    pub fn new(cache_dir: PathBuf) -> Arc<Self> {
        let session = hex::encode(rand::random::<[u8; 24]>());
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(6))
            .pool_idle_timeout(Duration::from_secs(60))
            .tcp_keepalive(Duration::from_secs(30))
            .build()
            .expect("http client");
        let _ = std::fs::create_dir_all(cache_dir.join("media"));
        Arc::new(Self { upstream: RwLock::new(None), snp: RwLock::new(None), session, client, cache_dir })
    }
}

pub async fn serve(shared: Arc<ProxyShared>) -> std::io::Result<u16> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();
    let app = Router::new()
        .route("/v1/events", any(ws_handler))
        .fallback(any(proxy_handler))
        // Uploads are streamed to the engine; the extractor default (2 MB) would break them over SNP.
        .layer(axum::extract::DefaultBodyLimit::disable())
        .with_state(shared.clone());
    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            log::error!("proxy stopped: {e}");
        }
    });
    let cache = shared.cache_dir.clone();
    tokio::task::spawn_blocking(move || evict_cache(&cache.join("media"), CACHE_LIMIT_BYTES));
    Ok(port)
}

pub(crate) fn cors(h: &mut HeaderMap) {
    h.insert("access-control-allow-origin", HeaderValue::from_static("*"));
    h.insert("access-control-expose-headers", HeaderValue::from_static("*"));
}

fn json_error(status: StatusCode, msg: &str) -> Response {
    let mut r = (status, axum::Json(serde_json::json!({ "detail": msg }))).into_response();
    cors(r.headers_mut());
    r
}

fn session_ok(shared: &ProxyShared, headers: &HeaderMap, query: Option<&str>) -> bool {
    let expected = shared.session.as_bytes();
    let provided = headers
        .get("x-sarness-session")
        .and_then(|v| v.to_str().ok())
        .map(String::from)
        .or_else(|| query_param(query, "s"));
    match provided {
        Some(p) => constant_eq(p.as_bytes(), expected),
        None => false,
    }
}

fn constant_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn query_param(query: Option<&str>, key: &str) -> Option<String> {
    query?.split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=')?;
        (k == key).then(|| percent_encoding::percent_decode_str(v).decode_utf8_lossy().into_owned())
    })
}

fn strip_session(query: Option<&str>) -> String {
    match query {
        None => String::new(),
        Some(q) => {
            let kept: Vec<&str> = q.split('&').filter(|kv| !kv.starts_with("s=") && !kv.is_empty()).collect();
            if kept.is_empty() { String::new() } else { format!("?{}", kept.join("&")) }
        }
    }
}

const HOP_BY_HOP: &[&str] = &[
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length", "x-sarness-session", "origin", "referer",
    "authorization",
];

async fn proxy_handler(State(shared): State<Arc<ProxyShared>>, req: Request) -> Response {
    let (parts, body) = req.into_parts();
    if parts.method == Method::OPTIONS {
        let mut r = StatusCode::NO_CONTENT.into_response();
        let h = r.headers_mut();
        cors(h);
        h.insert("access-control-allow-methods", HeaderValue::from_static("GET, POST, PUT, PATCH, DELETE, OPTIONS"));
        h.insert("access-control-allow-headers", HeaderValue::from_static("*"));
        h.insert("access-control-max-age", HeaderValue::from_static("600"));
        return r;
    }
    let query = parts.uri.query();
    let path = parts.uri.path().to_string();
    log::debug!("{} {}", parts.method, path);
    if path == "/__sarness/ping" {
        return json_error(StatusCode::OK, "pong");
    }
    if !session_ok(&shared, &parts.headers, query) {
        return json_error(StatusCode::FORBIDDEN, "invalid desktop session");
    }
    let upstream = shared.upstream.read().await.clone();

    // ---- immutable media cache --------------------------------------------
    if parts.method == Method::GET {
        if let Some(id) = media_id(&path) {
            return serve_media(&shared, upstream.as_ref(), &id, &parts.headers).await;
        }
    }

    if let Some(backend) = shared.snp.read().await.clone() {
        return adapter::handle(&backend, Request::from_parts(parts, body)).await;
    }
    let Some(up) = upstream else {
        return json_error(StatusCode::SERVICE_UNAVAILABLE, "not connected to a Sarness Engine");
    };
    let url = format!("{}{}{}", up.base, path, strip_session(query));
    let mut rb = shared.client.request(parts.method.clone(), &url).bearer_auth(&up.token);
    for (k, v) in parts.headers.iter() {
        if !HOP_BY_HOP.contains(&k.as_str()) {
            rb = rb.header(k, v);
        }
    }
    let stream = body.into_data_stream().map_err(std::io::Error::other);
    rb = rb.body(reqwest::Body::wrap_stream(stream));
    match rb.send().await {
        Ok(resp) => {
            let status = resp.status();
            let mut out = Response::builder().status(status.as_u16());
            if let Some(h) = out.headers_mut() {
                for (k, v) in resp.headers().iter() {
                    if !HOP_BY_HOP.contains(&k.as_str()) && !k.as_str().starts_with("access-control-") {
                        h.insert(k.clone(), v.clone());
                    }
                }
                cors(h);
            }
            out.body(Body::from_stream(resp.bytes_stream())).unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
        }
        Err(e) => {
            let msg = if e.is_connect() || e.is_timeout() {
                "Sarness Engine is unreachable"
            } else {
                "request to Sarness Engine failed"
            };
            log::warn!("proxy error for {path}: {e}");
            json_error(StatusCode::BAD_GATEWAY, msg)
        }
    }
}

fn media_id(path: &str) -> Option<String> {
    let rest = path.strip_prefix("/v1/files/")?;
    if rest.is_empty() || rest.contains('/') || rest.len() > 64 {
        return None;
    }
    rest.chars().all(|c| c.is_ascii_alphanumeric() || c == '_').then(|| rest.to_string())
}

fn ext_for(mime: &str) -> &'static str {
    match mime {
        "image/png" => "png",
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        "image/gif" => "gif",
        "video/mp4" => "mp4",
        "video/webm" => "webm",
        "video/quicktime" => "mov",
        "application/json" => "json",
        "application/pdf" => "pdf",
        "text/plain" | "text/markdown" | "text/csv" => "txt",
        _ => "bin",
    }
}

pub fn cached_media_path(cache_dir: &Path, id: &str) -> Option<PathBuf> {
    let dir = cache_dir.join("media");
    let rd = std::fs::read_dir(&dir).ok()?;
    for e in rd.flatten() {
        let name = e.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&format!("{id}.")) && !name.ends_with(".part") {
            return Some(e.path());
        }
    }
    None
}

pub async fn ensure_cached(shared: &ProxyShared, upstream: Option<&Upstream>, id: &str) -> Result<PathBuf, (StatusCode, String)> {
    if let Some(p) = cached_media_path(&shared.cache_dir, id) {
        let _ = filetime_touch(&p);
        return Ok(p);
    }
    if let Some(backend) = shared.snp.read().await.clone() {
        return fetch_via_snp(shared, &backend, id).await;
    }
    let up = upstream.ok_or((StatusCode::SERVICE_UNAVAILABLE, "offline and not cached".to_string()))?;
    let resp = shared
        .client
        .get(format!("{}/v1/files/{id}", up.base))
        .bearer_auth(&up.token)
        .send()
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("engine unreachable: {e}")))?;
    if !resp.status().is_success() {
        let code = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
        return Err((code, format!("engine returned {}", resp.status())));
    }
    let mime = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_string();
    let dir = shared.cache_dir.join("media");
    let final_path = dir.join(format!("{id}.{}", ext_for(&mime)));
    let tmp = dir.join(format!("{id}.{}.part", uuid::Uuid::new_v4().simple()));
    let mut file = tokio::fs::File::create(&tmp).await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let mut stream = resp.bytes_stream();
    use tokio::io::AsyncWriteExt;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;
        file.write_all(&chunk).await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    }
    file.flush().await.ok();
    drop(file);
    tokio::fs::rename(&tmp, &final_path).await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(final_path)
}

/// SNP transport: `file.get` into the same content-addressed cache the HTTP path fills.
async fn fetch_via_snp(shared: &ProxyShared, backend: &SnpBackend, id: &str) -> Result<PathBuf, (StatusCode, String)> {
    let dir = shared.cache_dir.join("media");
    let tmp = dir.join(format!("{id}.{}.part", uuid::Uuid::new_v4().simple()));
    let meta = match adapter::download_to_file(&backend.client, id, &tmp, None).await {
        Ok(m) => m,
        Err(e) => {
            let _ = tokio::fs::remove_file(&tmp).await;
            let code = StatusCode::from_u16(e.http_status()).unwrap_or(StatusCode::BAD_GATEWAY);
            return Err((code, e.to_string()));
        }
    };
    let final_path = dir.join(format!("{id}.{}", ext_for(&meta.mime)));
    tokio::fs::rename(&tmp, &final_path).await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(final_path)
}

async fn serve_media(shared: &ProxyShared, upstream: Option<&Upstream>, id: &str, headers: &HeaderMap) -> Response {
    match ensure_cached(shared, upstream, id).await {
        Ok(path) => {
            let mut req = Request::builder().uri("/").method(Method::GET);
            for name in ["range", "if-none-match", "if-modified-since", "if-range"] {
                if let Some(v) = headers.get(name) {
                    req = req.header(HeaderName::from_static(name), v.clone());
                }
            }
            let req = req.body(Body::empty()).unwrap();
            match tower_http::services::ServeFile::new(&path).oneshot(req).await {
                Ok(r) => {
                    let mut r = r.map(Body::new).into_response();
                    let h = r.headers_mut();
                    cors(h);
                    h.insert("cache-control", HeaderValue::from_static("private, max-age=31536000, immutable"));
                    r
                }
                Err(_) => json_error(StatusCode::INTERNAL_SERVER_ERROR, "cache read failed"),
            }
        }
        Err((code, msg)) => json_error(code, &msg),
    }
}

fn filetime_touch(p: &Path) -> std::io::Result<()> {
    let f = std::fs::OpenOptions::new().append(true).open(p)?;
    f.set_modified(SystemTime::now())
}

/// Keeps the media cache under `limit` bytes by removing least recently used files.
pub fn evict_cache(dir: &Path, limit: u64) -> u64 {
    let Ok(rd) = std::fs::read_dir(dir) else { return 0 };
    let mut files: Vec<(SystemTime, u64, PathBuf)> = rd
        .flatten()
        .filter_map(|e| {
            let md = e.metadata().ok()?;
            md.is_file().then(|| (md.modified().unwrap_or(SystemTime::UNIX_EPOCH), md.len(), e.path()))
        })
        .collect();
    // Stale partial downloads are always removed.
    files.retain(|(_, _, p)| {
        if p.extension().map(|e| e == "part").unwrap_or(false) {
            let _ = std::fs::remove_file(p);
            false
        } else {
            true
        }
    });
    let mut total: u64 = files.iter().map(|f| f.1).sum();
    let mut removed = 0;
    if total <= limit {
        return 0;
    }
    files.sort_by_key(|f| f.0);
    let target = limit * 8 / 10;
    for (_, size, path) in files {
        if total <= target {
            break;
        }
        if std::fs::remove_file(&path).is_ok() {
            total -= size;
            removed += size;
        }
    }
    removed
}

pub fn cache_size(dir: &Path) -> u64 {
    std::fs::read_dir(dir)
        .map(|rd| rd.flatten().filter_map(|e| e.metadata().ok()).filter(|m| m.is_file()).map(|m| m.len()).sum())
        .unwrap_or(0)
}

// ------------------------------------------------------------------- WebSocket
async fn ws_handler(State(shared): State<Arc<ProxyShared>>, ws: WebSocketUpgrade, req: Request) -> Response {
    let (parts, _) = req.into_parts();
    log::debug!("WS {}", parts.uri.path());
    if !session_ok(&shared, &parts.headers, parts.uri.query()) {
        return json_error(StatusCode::FORBIDDEN, "invalid desktop session");
    }
    if let Some(backend) = shared.snp.read().await.clone() {
        return adapter::ws(backend, ws).await;
    }
    let Some(up) = shared.upstream.read().await.clone() else {
        return json_error(StatusCode::SERVICE_UNAVAILABLE, "not connected to a Sarness Engine");
    };
    let ws_url = up.base.replacen("http://", "ws://", 1).replacen("https://", "wss://", 1) + "/v1/events";
    let mut upstream_req = match ws_url.as_str().into_client_request() {
        Ok(r) => r,
        Err(_) => return json_error(StatusCode::BAD_GATEWAY, "invalid engine URL"),
    };
    upstream_req
        .headers_mut()
        .insert("authorization", HeaderValue::from_str(&format!("Bearer {}", up.token)).unwrap());
    let upstream = match tokio::time::timeout(Duration::from_secs(8), tokio_tungstenite::connect_async(upstream_req)).await {
        Ok(Ok((s, _))) => s,
        _ => return json_error(StatusCode::BAD_GATEWAY, "Sarness Engine is unreachable"),
    };
    ws.on_upgrade(move |client| async move {
        let (mut up_tx, mut up_rx) = upstream.split();
        let (mut cl_tx, mut cl_rx) = client.split();
        let to_client = async {
            while let Some(Ok(m)) = up_rx.next().await {
                let out = match m {
                    tungstenite::Message::Text(t) => AxMsg::Text(t.as_str().into()),
                    tungstenite::Message::Binary(b) => AxMsg::Binary(b),
                    tungstenite::Message::Ping(p) => AxMsg::Ping(p),
                    tungstenite::Message::Pong(p) => AxMsg::Pong(p),
                    tungstenite::Message::Close(_) => break,
                    tungstenite::Message::Frame(_) => continue,
                };
                if cl_tx.send(out).await.is_err() {
                    break;
                }
            }
            let _ = cl_tx.send(AxMsg::Close(None)).await;
        };
        let to_upstream = async {
            while let Some(Ok(m)) = cl_rx.next().await {
                let out = match m {
                    AxMsg::Text(t) => tungstenite::Message::Text(t.as_str().into()),
                    AxMsg::Binary(b) => tungstenite::Message::Binary(b),
                    AxMsg::Ping(p) => tungstenite::Message::Ping(p),
                    AxMsg::Pong(p) => tungstenite::Message::Pong(p),
                    AxMsg::Close(_) => break,
                };
                if up_tx.send(out).await.is_err() {
                    break;
                }
            }
            let _ = up_tx.close().await;
        };
        tokio::select! { _ = to_client => {}, _ = to_upstream => {} }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn media_ids_are_strict() {
        assert_eq!(media_id("/v1/files/f_abc123"), Some("f_abc123".into()));
        assert_eq!(media_id("/v1/files/f_abc/sign"), None);
        assert_eq!(media_id("/v1/files/../etc"), None);
        assert_eq!(media_id("/v1/files/"), None);
    }

    #[test]
    fn session_query_is_stripped() {
        assert_eq!(strip_session(Some("s=abc&type=image")), "?type=image");
        assert_eq!(strip_session(Some("s=abc")), "");
        assert_eq!(query_param(Some("a=1&s=x%20y"), "s"), Some("x y".into()));
    }

    #[test]
    fn eviction_respects_limit() {
        let dir = tempfile::tempdir().unwrap();
        for i in 0..10 {
            std::fs::write(dir.path().join(format!("f_{i}.png")), vec![0u8; 1000]).unwrap();
        }
        std::fs::write(dir.path().join("f_x.png.part"), vec![0u8; 10]).unwrap();
        let removed = evict_cache(dir.path(), 5000);
        assert!(removed >= 5000);
        assert!(cache_size(dir.path()) <= 4000);
        assert!(!dir.path().join("f_x.png.part").exists());
    }

    #[test]
    fn constant_time_compare() {
        assert!(constant_eq(b"abc", b"abc"));
        assert!(!constant_eq(b"abc", b"abd"));
        assert!(!constant_eq(b"abc", b"ab"));
    }
}

#[cfg(test)]
mod integration {
    //! End-to-end tests of the proxy against a mock engine (no real server needed).
    use super::*;
    use axum::{extract::ws::WebSocketUpgrade as Wsu, http::HeaderMap as HM, routing::get, Json};

    const TOKEN: &str = "sk-sarness-test-token";
    const PNG: &[u8] = b"\x89PNG\r\n\x1a\nfakeimagebytes-0123456789";

    fn authed(h: &HM) -> bool {
        h.get("authorization").and_then(|v| v.to_str().ok()) == Some(&format!("Bearer {TOKEN}"))
    }

    async fn mock_engine() -> (u16, tokio::task::JoinHandle<()>) {
        let app = Router::new()
            .route("/v1/system", get(|h: HM| async move {
                if !authed(&h) { return (StatusCode::UNAUTHORIZED, Json(serde_json::json!({"detail":"nope"}))).into_response(); }
                // The session key must never be forwarded upstream.
                assert!(h.get("x-sarness-session").is_none());
                Json(serde_json::json!({"ok": true})).into_response()
            }))
            .route("/v1/files/f_img1", get(|h: HM| async move {
                if !authed(&h) { return StatusCode::UNAUTHORIZED.into_response(); }
                ([("content-type", "image/png")], PNG).into_response()
            }))
            .route("/v1/stream", axum::routing::post(|| async {
                let s = futures_util::stream::iter((0..3).map(|i| Ok::<_, std::io::Error>(bytes::Bytes::from(format!("data: {{\"i\":{i}}}\n\n")))));
                ([("content-type", "text/event-stream")], Body::from_stream(s)).into_response()
            }))
            .route("/v1/events", get(|h: HM, ws: Wsu| async move {
                if !authed(&h) { return StatusCode::UNAUTHORIZED.into_response(); }
                ws.on_upgrade(|mut s| async move {
                    let _ = s.send(AxMsg::Text("{\"type\":\"hello\"}".into())).await;
                    while let Some(Ok(AxMsg::Text(t))) = s.recv().await {
                        let _ = s.send(AxMsg::Text(format!("echo:{}", t.as_str()).into())).await;
                    }
                })
            }));
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = l.local_addr().unwrap().port();
        let h = tokio::spawn(async move { axum::serve(l, app).await.unwrap(); });
        (port, h)
    }

    #[tokio::test]
    async fn proxies_with_token_session_cache_ws_and_offline() {
        let (eport, engine) = mock_engine().await;
        let dir = tempfile::tempdir().unwrap();
        let shared = ProxyShared::new(dir.path().to_path_buf());
        let pport = serve(shared.clone()).await.unwrap();
        let base = format!("http://127.0.0.1:{pport}");
        let c = reqwest::Client::new();

        // Not connected yet.
        let r = c.get(format!("{base}/v1/system")).header("x-sarness-session", &shared.session).send().await.unwrap();
        assert_eq!(r.status(), 503);

        *shared.upstream.write().await = Some(Upstream { base: format!("http://127.0.0.1:{eport}"), token: TOKEN.into() });

        // Missing / wrong session key is refused before reaching the engine.
        assert_eq!(c.get(format!("{base}/v1/system")).send().await.unwrap().status(), 403);
        assert_eq!(c.get(format!("{base}/v1/system")).header("x-sarness-session", "wrong").send().await.unwrap().status(), 403);

        // Token is injected; a token sent by the client is not trusted/forwarded.
        let r = c.get(format!("{base}/v1/system?s={}", shared.session)).header("authorization", "Bearer evil").send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers().get("access-control-allow-origin").unwrap(), "*");
        assert_eq!(r.json::<serde_json::Value>().await.unwrap()["ok"], true);

        // Streaming bodies pass through.
        let body = c.post(format!("{base}/v1/stream")).header("x-sarness-session", &shared.session).send().await.unwrap().text().await.unwrap();
        assert_eq!(body.matches("data:").count(), 3);

        // Media is cached on disk and served with Range support.
        let url = format!("{base}/v1/files/f_img1?s={}", shared.session);
        let r = c.get(&url).send().await.unwrap();
        assert_eq!(r.headers().get("content-type").unwrap(), "image/png");
        assert_eq!(r.bytes().await.unwrap().as_ref(), PNG);
        assert!(cached_media_path(dir.path(), "f_img1").unwrap().to_string_lossy().ends_with(".png"));
        let r = c.get(&url).header("range", "bytes=0-3").send().await.unwrap();
        assert_eq!(r.status(), 206);
        assert_eq!(r.bytes().await.unwrap().as_ref(), &PNG[..4]);

        // WebSocket bridge (auth header added upstream).
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://127.0.0.1:{pport}/v1/events?s={}", shared.session)).await.unwrap();
        let hello = ws.next().await.unwrap().unwrap();
        assert_eq!(hello.into_text().unwrap().as_str(), "{\"type\":\"hello\"}");
        ws.send(tungstenite::Message::Text("ping".into())).await.unwrap();
        assert_eq!(ws.next().await.unwrap().unwrap().into_text().unwrap().as_str(), "echo:ping");
        drop(ws);

        // Engine goes away: API calls report offline, cached media still works.
        engine.abort();
        tokio::time::sleep(Duration::from_millis(100)).await;
        let r = c.get(format!("{base}/v1/system")).header("x-sarness-session", &shared.session).send().await.unwrap();
        assert_eq!(r.status(), 502);
        let r = c.get(&url).send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.bytes().await.unwrap().as_ref(), PNG);
        // Uncached media while offline is an honest error.
        let r = c.get(format!("{base}/v1/files/f_other?s={}", shared.session)).send().await.unwrap();
        assert_eq!(r.status(), 502);
    }
}
