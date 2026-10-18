//! Sarness desktop core: connection management, loopback proxy, SSH tunnels,
//! discovery, guided engine installation and local media export.

mod agent;
mod config;
mod discovery;
mod proxy;
mod snp;
mod ssh;

use config::{ConnectionMode, Profile, ProfileStore, Transport};
use proxy::{ProxyShared, Upstream};
use snp::adapter::SnpBackend;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::sync::Mutex;

const INSTALL_SCRIPT: &str = include_str!("../../../scripts/install-engine.sh");

struct TunnelHandle {
    task: tokio::task::JoinHandle<()>,
}

pub struct Core {
    proxy: Arc<ProxyShared>,
    proxy_port: u16,
    tunnel: Mutex<Option<TunnelHandle>>,
    active: Mutex<Option<String>>,
}

#[derive(Serialize)]
struct ProxyInfo {
    url: String,
    session: String,
    connected: bool,
    profile_id: Option<String>,
    /// Transport of the active profile: "snp" | "http" | "https".
    transport: Option<String>,
    /// Live SNP state (negotiated session, connection status) while an SNP profile is active.
    snp: Option<SnpLive>,
}

#[derive(Serialize)]
struct SnpLive {
    status: snp::client::ClientStatus,
    session: Option<snp::SessionInfo>,
}

/// Negotiated SNP parameters reported by "Test connection". Contains no secrets.
#[derive(Serialize)]
struct SnpTestInfo {
    version: u8,
    server: String,
    features: Vec<String>,
    limits: snp::client::Limits,
    api_version: String,
    pin: String,
    fingerprint: String,
    /// HELLO → HELLO_OK round trip.
    handshake_ms: u64,
}

#[derive(Serialize, Default)]
struct TestResult {
    ok: bool,
    stage: String,
    message: String,
    latency_ms: Option<u128>,
    health: Option<serde_json::Value>,
    system: Option<serde_json::Value>,
    /// "snp" | "http" | "https"
    transport: String,
    snp: Option<SnpTestInfo>,
    /// Machine-readable failure category (dns, refused, timeout, tls, pin_mismatch, …).
    error_kind: Option<String>,
    /// Pin stored in the profile / pin the server presented (pin mismatch or untrusted certificate).
    expected_pin: Option<String>,
    presented_pin: Option<String>,
}

type CmdResult<T> = Result<T, String>;

fn http() -> reqwest::Client {
    reqwest::Client::builder().connect_timeout(Duration::from_secs(6)).timeout(Duration::from_secs(15)).build().unwrap()
}

// ------------------------------------------------------------------ profiles
#[tauri::command]
fn list_profiles() -> ProfileStore {
    ProfileStore::load()
}

#[tauri::command]
fn save_profile(mut profile: Profile, token: Option<String>) -> CmdResult<Profile> {
    if profile.id.is_empty() {
        profile.id = uuid::Uuid::new_v4().simple().to_string();
    }
    profile.normalize();
    profile.validate()?;
    if let Some(t) = token.filter(|t| !t.trim().is_empty()) {
        config::set_token(&profile.id, t.trim())?;
    }
    profile.has_token = config::get_token(&profile.id).is_some();
    let mut st = ProfileStore::load();
    st.upsert(profile.clone());
    if st.active.is_none() {
        st.active = Some(profile.id.clone());
    }
    st.save().map_err(|e| e.to_string())?;
    Ok(profile)
}

#[tauri::command]
async fn delete_profile(core: State<'_, Core>, id: String) -> CmdResult<()> {
    if core.active.lock().await.as_deref() == Some(&id) {
        disconnect_inner(&core).await;
    }
    config::delete_token(&id);
    let mut st = ProfileStore::load();
    st.profiles.retain(|p| p.id != id);
    if st.active.as_deref() == Some(&id) {
        st.active = st.profiles.first().map(|p| p.id.clone());
    }
    st.save().map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- connection
async fn check_engine(base: &str, token: &str) -> TestResult {
    let client = http();
    let t0 = Instant::now();
    let health = match client.get(format!("{base}/v1/health")).send().await {
        Ok(r) => match r.json::<serde_json::Value>().await {
            Ok(v) if v.get("service").and_then(|s| s.as_str()) == Some("sarness-engine") => v,
            Ok(_) => return TestResult { stage: "health".into(), message: "Something answered on that port, but it is not a Sarness Engine.".into(), ..Default::default() },
            Err(_) => return TestResult { stage: "health".into(), message: "The server responded with an unexpected format.".into(), ..Default::default() },
        },
        Err(e) => {
            let msg = if e.is_timeout() || e.is_connect() {
                format!("Could not reach {base}. Check the address and port, that the engine is running, and that this computer can reach the server.")
            } else {
                format!("Connection failed: {e}")
            };
            return TestResult { stage: "connect".into(), message: msg, ..Default::default() };
        }
    };
    let latency = t0.elapsed().as_millis();
    let major = health.get("api_version").and_then(|v| v.as_str()).and_then(|v| v.split('.').next()).unwrap_or("");
    if major != "1" {
        return TestResult { stage: "version".into(), message: format!("Unsupported engine API version {major}; update Sarness."), health: Some(health), ..Default::default() };
    }
    match client.get(format!("{base}/v1/system")).bearer_auth(token).send().await {
        Ok(r) if r.status().as_u16() == 401 => TestResult { stage: "auth".into(), message: "The API token was rejected. Copy the token printed by 'sarness-engine init' (or issue a new one with 'sarness-engine token --rotate').".into(), health: Some(health), latency_ms: Some(latency), ..Default::default() },
        Ok(r) if r.status().is_success() => {
            let system = r.json().await.ok();
            TestResult { ok: true, stage: "ok".into(), message: "Connected".into(), latency_ms: Some(latency), health: Some(health), system, ..Default::default() }
        }
        Ok(r) => TestResult { stage: "system".into(), message: format!("Engine returned HTTP {}", r.status()), health: Some(health), ..Default::default() },
        Err(e) => TestResult { stage: "system".into(), message: format!("Engine request failed: {e}"), health: Some(health), ..Default::default() },
    }
}

fn parse_pin(profile: &Profile) -> Result<Option<snp::Pin>, String> {
    profile.snp_pin.as_deref().map(snp::Pin::parse).transpose()
}

fn snp_config(profile: &Profile, host: &str, port: u16, token: &str) -> Result<snp::SnpConfig, String> {
    let mut cfg = snp::SnpConfig::new(host, port, parse_pin(profile)?, token);
    cfg.endpoint.server_name = profile.snp_server_name.clone();
    Ok(cfg)
}

async fn check_snp(cfg: snp::SnpConfig) -> TestResult {
    let expected = cfg.pin.map(|p| p.to_string());
    match snp::check(cfg).await {
        Ok(o) => {
            let pin = snp::Pin::parse(&o.info.pin).ok();
            TestResult {
                ok: true,
                stage: "ok".into(),
                message: "Connected over SNP".into(),
                latency_ms: Some(o.rtt_ms),
                health: o.health,
                system: o.system,
                transport: "snp".into(),
                snp: Some(SnpTestInfo {
                    version: o.info.version,
                    server: o.info.server.clone(),
                    features: o.info.features.clone(),
                    limits: o.info.limits.clone(),
                    api_version: o.info.api_version.clone(),
                    fingerprint: pin.map(|p| p.fingerprint()).unwrap_or_default(),
                    pin: o.info.pin.clone(),
                    handshake_ms: o.info.latency_ms,
                }),
                ..Default::default()
            }
        }
        Err(e) => {
            let (exp, pres) = match &e {
                snp::SnpError::PinMismatch { expected, presented } => (Some(expected.to_string()), presented.map(|p| p.to_string())),
                snp::SnpError::UntrustedCertificate { presented, .. } => (expected, presented.map(|p| p.to_string())),
                _ => (None, None),
            };
            TestResult {
                stage: e.stage().into(),
                message: e.to_string(),
                transport: "snp".into(),
                error_kind: Some(e.kind().into()),
                expected_pin: exp,
                presented_pin: pres,
                ..Default::default()
            }
        }
    }
}

#[tauri::command]
async fn test_connection(mut profile: Profile, token: Option<String>) -> CmdResult<TestResult> {
    profile.normalize();
    profile.validate()?;
    let transport = profile.transport.as_str().to_string();
    let token = token.filter(|t| !t.trim().is_empty()).map(|t| t.trim().to_string()).or_else(|| config::get_token(&profile.id));
    let Some(token) = token else {
        return Ok(TestResult { stage: "auth".into(), message: "Enter the engine API token.".into(), transport, ..Default::default() });
    };
    let mut res = match (profile.transport, &profile.mode) {
        (Transport::Snp, ConnectionMode::Direct) => check_snp(snp_config(&profile, &profile.host, profile.snp_port, &token)?).await,
        (Transport::Snp, ConnectionMode::Ssh) => {
            let mut tunnel = match ssh::open_profile_tunnel(&profile).await {
                Ok(t) => t,
                Err(e) => return Ok(TestResult { stage: "ssh".into(), message: e, transport, error_kind: Some("ssh".into()), ..Default::default() }),
            };
            let res = check_snp(snp_config(&profile, "127.0.0.1", tunnel.local_port, &token)?).await;
            let _ = tunnel.child.kill().await;
            res
        }
        (_, ConnectionMode::Direct) => check_engine(&profile.direct_url(), &token).await,
        (_, ConnectionMode::Ssh) => {
            let mut tunnel = match ssh::open_profile_tunnel(&profile).await {
                Ok(t) => t,
                Err(e) => return Ok(TestResult { stage: "ssh".into(), message: e, transport, ..Default::default() }),
            };
            let res = check_engine(&format!("http://127.0.0.1:{}", tunnel.local_port), &token).await;
            let _ = tunnel.child.kill().await;
            res
        }
    };
    res.transport = transport;
    Ok(res)
}

/// Trust-on-first-use for SNP: connects once, only to read the identity the server
/// presents (no AUTH is sent). The UI shows the fingerprint and stores it in the
/// profile only after the user confirms.
#[tauri::command]
async fn snp_probe_pin(mut profile: Profile) -> CmdResult<snp::ProbeResult> {
    profile.normalize();
    profile.snp_pin = None;
    profile.transport = Transport::Snp;
    profile.validate()?;
    let name = profile.snp_server_name.clone();
    match profile.mode {
        ConnectionMode::Direct => snp::probe(&profile.host, profile.snp_port, name.as_deref()).await.map_err(|e| e.to_string()),
        ConnectionMode::Ssh => {
            let mut tunnel = ssh::open_profile_tunnel(&profile).await?;
            let res = snp::probe("127.0.0.1", tunnel.local_port, name.as_deref()).await.map_err(|e| e.to_string());
            let _ = tunnel.child.kill().await;
            res
        }
    }
}

async fn disconnect_inner(core: &Core) {
    if let Some(t) = core.tunnel.lock().await.take() {
        t.task.abort();
    }
    *core.proxy.upstream.write().await = None;
    if let Some(b) = core.proxy.snp.write().await.take() {
        b.client.close().await;
    }
    *core.active.lock().await = None;
}

/// What a restarted SSH tunnel must update.
#[derive(Clone)]
enum TunnelTarget {
    Http { proxy: Arc<ProxyShared>, token: String },
    Snp { client: snp::SnpClient },
}

impl TunnelTarget {
    async fn point_at(&self, local_port: u16) {
        match self {
            TunnelTarget::Http { proxy, token } => {
                *proxy.upstream.write().await = Some(Upstream { base: format!("http://127.0.0.1:{local_port}"), token: token.clone() });
            }
            TunnelTarget::Snp { client } => client.set_endpoint("127.0.0.1", local_port),
        }
    }
}

#[tauri::command]
async fn connect(app: AppHandle, core: State<'_, Core>, id: String) -> CmdResult<ProxyInfo> {
    let store = ProfileStore::load();
    let profile = store.get(&id).cloned().ok_or("profile not found")?;
    let token = config::get_token(&id).ok_or("no API token stored for this server")?;
    disconnect_inner(&core).await;

    // Exactly one of `proxy.upstream` (HTTP transports) / `proxy.snp` is set, so an SNP
    // profile can never be served by an HTTP fallback.
    let snp_client = if profile.transport == Transport::Snp {
        let (host, port) = match profile.mode {
            ConnectionMode::Direct => (profile.host.clone(), profile.snp_port),
            ConnectionMode::Ssh => ("127.0.0.1".to_string(), 0), // replaced once the tunnel is up
        };
        Some(snp::SnpClient::new(snp_config(&profile, &host, port, &token)?))
    } else {
        None
    };
    let set_snp_backend = |client: &snp::SnpClient| {
        Arc::new(SnpBackend { client: client.clone(), profile_id: id.clone() })
    };

    match profile.mode {
        ConnectionMode::Direct => {
            if let Some(client) = &snp_client {
                *core.proxy.snp.write().await = Some(set_snp_backend(client));
            } else {
                *core.proxy.upstream.write().await = Some(Upstream { base: profile.direct_url(), token });
            }
        }
        ConnectionMode::Ssh => {
            let first = ssh::open_profile_tunnel(&profile).await?;
            let target = match &snp_client {
                Some(client) => TunnelTarget::Snp { client: client.clone() },
                None => TunnelTarget::Http { proxy: core.proxy.clone(), token: token.clone() },
            };
            target.point_at(first.local_port).await;
            if let Some(client) = &snp_client {
                *core.proxy.snp.write().await = Some(set_snp_backend(client));
            }
            let app2 = app.clone();
            let profile2 = profile.clone();
            // Supervise the tunnel: restart with backoff whenever ssh exits.
            let task = tokio::spawn(async move {
                let mut tunnel = first;
                let mut backoff = 1u64;
                loop {
                    let _ = tunnel.child.wait().await;
                    let _ = app2.emit("tunnel-status", serde_json::json!({"state": "reconnecting"}));
                    loop {
                        tokio::time::sleep(Duration::from_secs(backoff)).await;
                        match ssh::open_profile_tunnel(&profile2).await {
                            Ok(t) => {
                                target.point_at(t.local_port).await;
                                let _ = app2.emit("tunnel-status", serde_json::json!({"state": "up"}));
                                tunnel = t;
                                backoff = 1;
                                break;
                            }
                            Err(e) => {
                                let _ = app2.emit("tunnel-status", serde_json::json!({"state": "down", "error": e}));
                                backoff = (backoff * 2).min(30);
                            }
                        }
                    }
                }
            });
            *core.tunnel.lock().await = Some(TunnelHandle { task });
        }
    }
    *core.active.lock().await = Some(id.clone());
    let mut st = ProfileStore::load();
    st.active = Some(id.clone());
    let _ = st.save();

    // Connect eagerly so identity / authentication failures are reported right away.
    // Plain unreachability is left to the background reconnect (like the HTTP transports).
    if let Some(client) = &snp_client {
        if let Ok(Err(e)) = tokio::time::timeout(Duration::from_secs(12), client.connect()).await {
            if e.is_fatal() {
                return Err(e.to_string());
            }
        }
    }
    Ok(proxy_info_inner(&core).await)
}

#[tauri::command]
async fn disconnect(core: State<'_, Core>) -> CmdResult<()> {
    disconnect_inner(&core).await;
    Ok(())
}

async fn proxy_info_inner(core: &Core) -> ProxyInfo {
    let snp_backend = core.proxy.snp.read().await.clone();
    let profile_id = core.active.lock().await.clone();
    let transport = match (&snp_backend, &profile_id) {
        (Some(_), _) => Some("snp".to_string()),
        (None, Some(id)) => ProfileStore::load().get(id).map(|p| p.transport.as_str().to_string()),
        _ => None,
    };
    let snp = match &snp_backend {
        Some(b) => Some(SnpLive { status: b.client.status(), session: b.client.info().await.map(|i| (*i).clone()) }),
        None => None,
    };
    ProxyInfo {
        url: format!("http://127.0.0.1:{}", core.proxy_port),
        session: core.proxy.session.clone(),
        connected: core.proxy.upstream.read().await.is_some() || snp_backend.is_some(),
        profile_id,
        transport,
        snp,
    }
}

#[tauri::command]
async fn proxy_info(core: State<'_, Core>) -> CmdResult<ProxyInfo> {
    Ok(proxy_info_inner(&core).await)
}

#[tauri::command]
async fn discover_servers() -> CmdResult<Vec<discovery::Candidate>> {
    Ok(discovery::discover().await)
}

// ----------------------------------------------------------------- installer
#[derive(Deserialize)]
struct InstallOptions {
    /// "tailscale" | "loopback" | "lan"
    bind: String,
    install_llama: bool,
    port: Option<u16>,
}

#[derive(Serialize, Clone)]
struct InstallEvent {
    stage: String,
    line: String,
}

fn engine_bundle(app: &AppHandle) -> Result<PathBuf, String> {
    if let Ok(dir) = app.path().resource_dir() {
        let p = dir.join("engine").join("sarness-engine.tar.gz");
        if p.exists() {
            return Ok(p);
        }
    }
    // Development fallback: package the engine directory from the source tree.
    let src = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../engine");
    if !src.join("pyproject.toml").exists() {
        return Err("engine bundle not found in this build".into());
    }
    let out = std::env::temp_dir().join(format!("sarness-engine-{}.tar.gz", std::process::id()));
    let status = std::process::Command::new("tar")
        .args(["--exclude=.venv", "--exclude=__pycache__", "--exclude=.pytest_cache", "--exclude=*.egg-info", "-czf"])
        .arg(&out)
        .arg("-C")
        .arg(src.parent().unwrap())
        .arg("engine")
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("failed to package the engine".into());
    }
    Ok(out)
}

#[tauri::command]
async fn install_engine(app: AppHandle, profile: Profile, options: InstallOptions) -> CmdResult<serde_json::Value> {
    let ssh_opts = profile.ssh.clone().ok_or("SSH details are required for guided installation")?;
    if !matches!(options.bind.as_str(), "tailscale" | "loopback" | "lan") {
        return Err("invalid bind option".into());
    }
    let emit = |stage: &str, line: &str| {
        let _ = app.emit("installer", InstallEvent { stage: stage.into(), line: line.into() });
    };
    emit("preflight", &format!("Connecting to {}@{} …", ssh_opts.user, ssh_opts.host));
    let pre = "echo \"host: $(hostname)\"; . /etc/os-release 2>/dev/null && echo \"os: $PRETTY_NAME\"; \
               echo \"python: $(python3 --version 2>&1)\"; \
               (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/gpu: /') || echo 'gpu: none detected'; \
               echo \"disk free: $(df -h \"$HOME\" | awk 'NR==2{print $4}')\"; mkdir -p \"$HOME/.cache\"";
    let code = ssh::run_remote(&ssh_opts, pre, None, |l| emit("preflight", l)).await?;
    if code != 0 {
        return Err("preflight checks failed on the server".into());
    }
    emit("upload", "Packaging and uploading Sarness Engine …");
    let bundle = engine_bundle(&app)?;
    ssh::copy_to(&ssh_opts, &bundle, ".cache/sarness-engine-src.tar.gz").await?;
    emit("install", "Running the installer (this downloads PyTorch and can take several minutes) …");
    let port = options.port.unwrap_or(discovery::ENGINE_PORT);
    let cmd = format!(
        "bash -s -- --source \"$HOME/.cache/sarness-engine-src.tar.gz\" --bind {} --port {}{} --json-result",
        options.bind,
        port,
        if options.install_llama { " --with-llama" } else { "" }
    );
    let mut result: Option<serde_json::Value> = None;
    let code = ssh::run_remote(&ssh_opts, &cmd, Some(INSTALL_SCRIPT.as_bytes()), |l| {
        if let Some(json) = l.strip_prefix("SARNESS_RESULT ") {
            result = serde_json::from_str(json).ok();
            emit("install", "Engine configured.");
        } else {
            emit("install", l);
        }
    })
    .await?;
    if code != 0 {
        return Err(format!("installer exited with status {code} — see the log above"));
    }
    let mut res = result.ok_or("installer did not report a result")?;
    if let Some(token) = res.get("token").and_then(|t| t.as_str()).filter(|t| !t.is_empty()) {
        config::set_token(&profile.id, token)?;
    }
    // Never hand the token back to the webview.
    if let Some(obj) = res.as_object_mut() {
        obj.remove("token");
    }
    emit("done", "Installation complete.");
    Ok(res)
}

// --------------------------------------------------------------------- media
fn default_export_dir(kind: &str) -> PathBuf {
    let base = if kind == "video" { dirs::video_dir() } else { dirs::picture_dir() };
    base.unwrap_or_else(|| dirs::home_dir().unwrap_or_default()).join("Sarness")
}

fn safe_name(name: &str) -> String {
    let n: String = name.chars().map(|c| if c.is_alphanumeric() || "-_. ".contains(c) { c } else { '_' }).collect();
    let n = n.trim_matches(|c| c == '.' || c == ' ').to_string();
    if n.is_empty() { "sarness-output".into() } else { n.chars().take(120).collect() }
}

#[tauri::command]
async fn export_file(core: State<'_, Core>, file_id: String, dest: String) -> CmdResult<String> {
    let up = core.proxy.upstream.read().await.clone();
    let src = proxy::ensure_cached(&core.proxy, up.as_ref(), &file_id).await.map_err(|e| e.1)?;
    tokio::fs::copy(&src, &dest).await.map_err(|e| e.to_string())?;
    Ok(dest)
}

#[tauri::command]
async fn save_to_library_folder(core: State<'_, Core>, file_id: String, kind: String, name: String) -> CmdResult<String> {
    let up = core.proxy.upstream.read().await.clone();
    let src = proxy::ensure_cached(&core.proxy, up.as_ref(), &file_id).await.map_err(|e| e.1)?;
    let dir = default_export_dir(&kind);
    tokio::fs::create_dir_all(&dir).await.map_err(|e| e.to_string())?;
    let ext = src.extension().map(|e| e.to_string_lossy().into_owned()).unwrap_or_else(|| "bin".into());
    let mut dest = dir.join(format!("{}.{}", safe_name(&name), ext));
    let mut i = 1;
    while dest.exists() {
        dest = dir.join(format!("{}-{}.{}", safe_name(&name), i, ext));
        i += 1;
    }
    tokio::fs::copy(&src, &dest).await.map_err(|e| e.to_string())?;
    Ok(dest.to_string_lossy().into_owned())
}

#[tauri::command]
async fn write_text_file(dest: String, contents: String) -> CmdResult<()> {
    tokio::fs::write(&dest, contents).await.map_err(|e| e.to_string())
}

#[tauri::command]
async fn read_text_file(path: String) -> CmdResult<String> {
    let md = tokio::fs::metadata(&path).await.map_err(|e| e.to_string())?;
    if md.len() > 20 * 1024 * 1024 {
        return Err("file is too large".into());
    }
    tokio::fs::read_to_string(&path).await.map_err(|e| e.to_string())
}

#[tauri::command]
fn cache_info() -> serde_json::Value {
    let dir = config::cache_dir().join("media");
    serde_json::json!({"path": dir, "bytes": proxy::cache_size(&dir), "limit_bytes": proxy::CACHE_LIMIT_BYTES})
}

#[tauri::command]
fn clear_cache() -> CmdResult<u64> {
    let dir = config::cache_dir().join("media");
    let freed = proxy::evict_cache(&dir, 0);
    Ok(freed)
}

/// Frontend errors and lifecycle messages end up in the desktop log (stderr / journal).
#[tauri::command]
fn frontend_log(level: String, message: String) {
    let msg: String = message.chars().take(2000).collect();
    match level.as_str() {
        "error" => log::error!(target: "webview", "{msg}"),
        "warn" => log::warn!(target: "webview", "{msg}"),
        _ => log::info!(target: "webview", "{msg}"),
    }
}

#[tauri::command]
fn app_info() -> serde_json::Value {
    serde_json::json!({
        "version": env!("CARGO_PKG_VERSION"),
        "config_dir": config::config_dir(),
        "cache_dir": config::cache_dir(),
        "export_images": default_export_dir("image"),
        "export_videos": default_export_dir("video"),
    })
}

// ----------------------------------------------------------------------- run
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info,tao=warn,wry=warn")).init();
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    let shared = ProxyShared::new(config::cache_dir());
    let port = rt.block_on(proxy::serve(shared.clone())).expect("start loopback proxy");
    tauri::async_runtime::set(rt.handle().clone());
    log::info!("loopback proxy on 127.0.0.1:{port}");
    let core = Core { proxy: shared, proxy_port: port, tunnel: Mutex::new(None), active: Mutex::new(None) };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(core)
        .manage(agent::commands::AgentState::new(config::config_dir().join("projects.json")))
        .on_page_load(|webview, payload| {
            log::info!("page {:?}: {}", payload.event(), webview.url().map(|u| u.to_string()).unwrap_or_default());
        })
        .invoke_handler(tauri::generate_handler![
            list_profiles, save_profile, delete_profile, test_connection, connect, disconnect, proxy_info,
            discover_servers, install_engine, export_file, save_to_library_folder, write_text_file,
            read_text_file, snp_probe_pin, cache_info, clear_cache, app_info, frontend_log,
            agent::commands::agent_tool_defs, agent::commands::agent_call, agent::commands::agent_cancel,
            agent::commands::agent_end_run, agent::commands::agent_reset_run, agent::commands::projects_list,
            agent::commands::projects_add, agent::commands::projects_remove, agent::commands::project_create,
            agent::commands::project_tree, agent::commands::project_git, agent::commands::project_diff,
            agent::commands::project_snapshot
        ])
        .build(tauri::generate_context!())
        .expect("error while building Sarness")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // Never leave dev servers or other agent children running after the app closes.
                let engine = app.state::<agent::commands::AgentState>().engine.clone();
                tauri::async_runtime::block_on(engine.shutdown());
            }
        });
    drop(rt);
}
