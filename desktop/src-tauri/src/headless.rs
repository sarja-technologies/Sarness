//! Headless loopback proxy for end-to-end tests and benchmarks (`cargo run --example headless_proxy`).
//!
//! It wires the real proxy exactly as the `connect` command does, for a stored profile, and writes
//! `<port> <session>` to a file so scripts can drive the same loopback interface the webview uses.
//! The API token is read from the keyring like the app does and is never printed.

use crate::config::{self, ConnectionMode, ProfileStore, SshOptions, Transport};
use crate::proxy::{self, Backend, ProxyShared, Upstream};
use crate::snp::adapter::SnpBackend;
use crate::{snp, ssh};
use std::sync::Arc;

pub async fn run(args: Vec<String>) -> Result<(), String> {
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned());
    let store = ProfileStore::load();
    // Several `--profile` flags connect several engines at once (reach them at /e/<id>/v1/...;
    // the first is primary). Per-profile overrides below apply to the primary only.
    let mut ids: Vec<String> = args.windows(2).filter(|w| w[0] == "--profile").map(|w| w[1].clone()).collect();
    if ids.is_empty() {
        ids.extend(store.active.clone());
    }
    let shared = ProxyShared::new(config::cache_dir());
    let mut tunnels = Vec::new();
    for (n, id) in ids.iter().enumerate() {
        let profile = store.get(id).cloned().ok_or(format!("no such profile {id}"))?;
        let t = connect_profile(&shared, profile, if n == 0 { Some(&flag) } else { None }).await?;
        tunnels.extend(t);
    }
    let out = flag("--out").ok_or("--out <file> is required")?;
    let listen = proxy::serve(shared.clone()).await.map_err(|e| e.to_string())?;
    std::fs::write(&out, format!("{listen} {}\n", shared.session)).map_err(|e| e.to_string())?;
    eprintln!("headless proxy ready on 127.0.0.1:{listen} (engines: {})", ids.join(", "));
    tokio::signal::ctrl_c().await.ok();
    drop(tunnels);
    Ok(())
}

async fn connect_profile(
    shared: &ProxyShared,
    mut profile: config::Profile,
    overrides: Option<&dyn Fn(&str) -> Option<String>>,
) -> Result<Option<ssh::Tunnel>, String> {
    let none = |_: &str| None;
    let flag: &dyn Fn(&str) -> Option<String> = overrides.unwrap_or(&none);
    let token = config::get_token(&profile.id).ok_or("no API token stored for this profile")?;
    profile.transport = match flag("--transport").as_deref() {
        Some("snp") => Transport::Snp,
        Some("http") => Transport::Http,
        None => profile.transport,
        Some(o) => return Err(format!("unknown transport {o}")),
    };
    if let Some(p) = flag("--pin") {
        profile.snp_pin = Some(p);
    }
    if let Some(p) = flag("--snp-port") {
        profile.snp_port = p.parse().map_err(|_| "bad --snp-port")?;
    }
    if let Some(user) = flag("--ssh-user") {
        profile.mode = ConnectionMode::Ssh;
        profile.ssh = Some(SshOptions { user, host: profile.host.clone(), port: 22, key_path: None });
    }
    let mut _tunnel = None;
    let (host, port) = if profile.mode == ConnectionMode::Ssh {
        let t = ssh::open_profile_tunnel(&profile).await?;
        let p = t.local_port;
        _tunnel = Some(t);
        ("127.0.0.1".to_string(), p)
    } else {
        (profile.host.clone(), profile.active_port())
    };
    if profile.transport == Transport::Snp {
        let client = snp::SnpClient::new(crate::snp_config(&profile, &host, port, &token)?);
        let info = client.connect().await.map_err(|e| e.to_string())?;
        eprintln!("SNP connected: server {} features {:?}", info.server, info.features);
        shared.set_engine(&profile.id, Some(Backend::Snp(Arc::new(SnpBackend { client })))).await;
    } else {
        let base = if profile.mode == ConnectionMode::Ssh { format!("{}://127.0.0.1:{port}", profile.scheme) } else { profile.direct_url() };
        shared.set_engine(&profile.id, Some(Backend::Http(Upstream { base, token }))).await;
    }
    eprintln!("connected {} ({:?}, {:?})", profile.id, profile.transport, profile.mode);
    Ok(_tunnel)
}
