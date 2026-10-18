//! Connection profiles (non-secret, JSON on disk) and API tokens (OS keyring,
//! with a 0600 file fallback when no Secret Service is available).

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::PathBuf;

const KEYRING_SERVICE: &str = "sarness";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionMode {
    Direct,
    Ssh,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SshOptions {
    pub user: String,
    pub host: String,
    #[serde(default = "default_ssh_port")]
    pub port: u16,
    #[serde(default)]
    pub key_path: Option<String>,
}

fn default_ssh_port() -> u16 {
    22
}

/// Application protocol spoken to the engine. Independent of [`ConnectionMode`]
/// (how the bytes get there): an SSH tunnel can carry any of them.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Transport {
    /// SNP/1: the native binary protocol over TLS 1.3 (see docs/SNP.md).
    Snp,
    /// HTTP/JSON API, plain.
    #[default]
    Http,
    /// HTTP/JSON API over TLS.
    Https,
}

impl Transport {
    pub fn as_str(&self) -> &'static str {
        match self {
            Transport::Snp => "snp",
            Transport::Http => "http",
            Transport::Https => "https",
        }
    }
}

pub const DEFAULT_SNP_PORT: u16 = 8744;

fn default_snp_port() -> u16 {
    DEFAULT_SNP_PORT
}

/// Wire / on-disk shape. Profiles saved before SNP existed only have `scheme`, so
/// `transport` is optional here and derived from it.
#[derive(Deserialize)]
struct ProfileRaw {
    id: String,
    name: String,
    host: String,
    port: u16,
    #[serde(default = "default_scheme")]
    scheme: String,
    mode: ConnectionMode,
    #[serde(default)]
    ssh: Option<SshOptions>,
    #[serde(default)]
    has_token: bool,
    #[serde(default)]
    transport: Option<Transport>,
    #[serde(default = "default_snp_port")]
    snp_port: u16,
    #[serde(default)]
    snp_pin: Option<String>,
    #[serde(default)]
    snp_server_name: Option<String>,
}

impl From<ProfileRaw> for Profile {
    fn from(r: ProfileRaw) -> Profile {
        let transport = r.transport.unwrap_or(if r.scheme == "https" { Transport::Https } else { Transport::Http });
        Profile {
            id: r.id,
            name: r.name,
            host: r.host,
            port: r.port,
            scheme: r.scheme,
            mode: r.mode,
            ssh: r.ssh,
            has_token: r.has_token,
            transport,
            snp_port: r.snp_port,
            snp_pin: r.snp_pin,
            snp_server_name: r.snp_server_name,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(from = "ProfileRaw")]
pub struct Profile {
    pub id: String,
    pub name: String,
    /// Hostname or IP address (MagicDNS names and 100.x addresses work too). For SSH
    /// mode this is the address the engine listens on *as seen from the server*
    /// (usually 127.0.0.1).
    pub host: String,
    /// Port of the HTTP/JSON API (http and https transports).
    pub port: u16,
    /// Legacy field kept for compatibility (`http` or `https`); mirrors `transport`
    /// for the HTTP transports.
    pub scheme: String,
    pub mode: ConnectionMode,
    pub ssh: Option<SshOptions>,
    pub has_token: bool,
    pub transport: Transport,
    /// SNP port on the server (default 8744).
    pub snp_port: u16,
    /// Pinned server identity, `sha256:<64 hex>` of the certificate's SPKI.
    pub snp_pin: Option<String>,
    /// Name to verify against a CA-issued certificate (only used without a pin).
    pub snp_server_name: Option<String>,
}

fn default_scheme() -> String {
    "http".into()
}

impl Profile {
    /// Port the client connects to for the active transport (the remote side when tunnelled).
    pub fn active_port(&self) -> u16 {
        if self.transport == Transport::Snp { self.snp_port } else { self.port }
    }

    /// Brings derived/legacy fields in line and trims user input.
    pub fn normalize(&mut self) {
        self.scheme = if self.transport == Transport::Https { "https".into() } else { "http".into() };
        self.snp_pin = self.snp_pin.take().map(|p| p.trim().to_lowercase()).filter(|p| !p.is_empty());
        self.snp_server_name = self.snp_server_name.take().map(|n| n.trim().to_string()).filter(|n| !n.is_empty());
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.name.trim().is_empty() {
            return Err("profile name is required".into());
        }
        if self.host.trim().is_empty() || self.host.contains(['/', ' ', '@']) {
            return Err("enter a hostname or IP address without scheme or path".into());
        }
        if self.active_port() == 0 {
            return Err("port must be between 1 and 65535".into());
        }
        if !matches!(self.scheme.as_str(), "http" | "https") {
            return Err("scheme must be http or https".into());
        }
        if self.transport == Transport::Snp {
            if let Some(p) = self.snp_pin.as_deref() {
                crate::snp::tls::Pin::parse(p)?;
            }
            if let Some(n) = self.snp_server_name.as_deref() {
                if n.contains(['/', ' ', '@']) {
                    return Err("the server name must be a plain host name".into());
                }
            }
        }
        if self.mode == ConnectionMode::Ssh {
            let ssh = self.ssh.as_ref().ok_or("SSH details are required for SSH mode")?;
            if ssh.user.trim().is_empty() || ssh.host.trim().is_empty() {
                return Err("SSH user and host are required".into());
            }
            if ssh.user.starts_with('-') || ssh.host.starts_with('-') {
                return Err("invalid SSH user or host".into());
            }
        }
        Ok(())
    }

    /// The engine base URL reachable from this machine (before any tunnel).
    pub fn direct_url(&self) -> String {
        let host = if self.host.contains(':') && !self.host.starts_with('[') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        format!("{}://{}:{}", self.scheme, host, self.port)
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ProfileStore {
    #[serde(default)]
    pub profiles: Vec<Profile>,
    #[serde(default)]
    pub active: Option<String>,
}

pub fn config_dir() -> PathBuf {
    let d = dirs::config_dir().unwrap_or_else(|| PathBuf::from(".")).join("sarness");
    let _ = fs::create_dir_all(&d);
    d
}

pub fn cache_dir() -> PathBuf {
    let d = dirs::cache_dir().unwrap_or_else(std::env::temp_dir).join("sarness");
    let _ = fs::create_dir_all(&d);
    d
}

fn profiles_path() -> PathBuf {
    config_dir().join("profiles.json")
}

impl ProfileStore {
    pub fn load() -> Self {
        fs::read_to_string(profiles_path())
            .ok()
            .and_then(|s| serde_json::from_str::<ProfileStore>(&s).ok())
            .map(|mut st| {
                for p in st.profiles.iter_mut() {
                    p.has_token = get_token(&p.id).is_some();
                }
                st
            })
            .unwrap_or_default()
    }

    pub fn save(&self) -> std::io::Result<()> {
        write_private(&profiles_path(), serde_json::to_string_pretty(self)?.as_bytes())
    }

    pub fn upsert(&mut self, p: Profile) {
        if let Some(existing) = self.profiles.iter_mut().find(|x| x.id == p.id) {
            *existing = p;
        } else {
            self.profiles.push(p);
        }
    }

    pub fn get(&self, id: &str) -> Option<&Profile> {
        self.profiles.iter().find(|p| p.id == id)
    }
}

pub fn write_private(path: &PathBuf, data: &[u8]) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    {
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts.open(&tmp)?;
        f.write_all(data)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)
}

// ------------------------------------------------------------------ secrets
fn fallback_path() -> PathBuf {
    config_dir().join("secrets.json")
}

fn fallback_load() -> serde_json::Map<String, serde_json::Value> {
    fs::read_to_string(fallback_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn set_token(profile_id: &str, token: &str) -> Result<(), String> {
    match keyring::Entry::new(KEYRING_SERVICE, profile_id).and_then(|e| e.set_password(token)) {
        Ok(()) => {
            // Make sure no stale fallback copy lingers.
            let mut m = fallback_load();
            if m.remove(profile_id).is_some() {
                let _ = write_private(&fallback_path(), serde_json::to_string(&m).unwrap().as_bytes());
            }
            Ok(())
        }
        Err(e) => {
            log::warn!("keyring unavailable ({e}); storing token in a private file");
            let mut m = fallback_load();
            m.insert(profile_id.to_string(), serde_json::Value::String(token.to_string()));
            write_private(&fallback_path(), serde_json::to_string(&m).unwrap().as_bytes()).map_err(|e| e.to_string())
        }
    }
}

pub fn get_token(profile_id: &str) -> Option<String> {
    if let Ok(e) = keyring::Entry::new(KEYRING_SERVICE, profile_id) {
        if let Ok(t) = e.get_password() {
            return Some(t);
        }
    }
    fallback_load().get(profile_id).and_then(|v| v.as_str().map(String::from))
}

pub fn delete_token(profile_id: &str) {
    if let Ok(e) = keyring::Entry::new(KEYRING_SERVICE, profile_id) {
        let _ = e.delete_credential();
    }
    let mut m = fallback_load();
    if m.remove(profile_id).is_some() {
        let _ = write_private(&fallback_path(), serde_json::to_string(&m).unwrap().as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> Profile {
        Profile {
            id: "p1".into(),
            name: "AI server".into(),
            host: "100.64.0.5".into(),
            port: 8742,
            scheme: "http".into(),
            mode: ConnectionMode::Direct,
            ssh: None,
            has_token: false,
            transport: Transport::Http,
            snp_port: DEFAULT_SNP_PORT,
            snp_pin: None,
            snp_server_name: None,
        }
    }

    /// A profiles.json written by Sarness 0.1 (before transports existed).
    const OLD_PROFILES: &str = r#"{
      "profiles": [
        {"id":"a1","name":"Home GPU","host":"100.64.0.5","port":8742,"scheme":"http","mode":"direct","ssh":null,"has_token":true},
        {"id":"b2","name":"Over SSH","host":"127.0.0.1","port":8742,"scheme":"http","mode":"ssh",
         "ssh":{"user":"me","host":"box.example","port":2222,"key_path":"/k"},"has_token":true},
        {"id":"c3","name":"TLS","host":"ai.example.com","port":8443,"scheme":"https","mode":"direct"}
      ],
      "active": "b2"
    }"#;

    #[test]
    fn old_profiles_load_unchanged() {
        let st: ProfileStore = serde_json::from_str(OLD_PROFILES).unwrap();
        assert_eq!(st.profiles.len(), 3);
        assert_eq!(st.active.as_deref(), Some("b2"));
        let a = st.get("a1").unwrap();
        assert_eq!((a.transport, a.scheme.as_str(), a.port, a.mode.clone()), (Transport::Http, "http", 8742, ConnectionMode::Direct));
        assert_eq!(a.snp_port, 8744);
        assert!(a.snp_pin.is_none() && a.snp_server_name.is_none());
        assert!(a.has_token);
        let b = st.get("b2").unwrap();
        assert_eq!(b.transport, Transport::Http);
        assert_eq!(b.mode, ConnectionMode::Ssh);
        assert_eq!(b.ssh.as_ref().unwrap().port, 2222);
        assert_eq!(b.direct_url(), "http://127.0.0.1:8742");
        let c = st.get("c3").unwrap();
        assert_eq!(c.transport, Transport::Https);
        assert_eq!(c.direct_url(), "https://ai.example.com:8443");
        assert!(!c.has_token);
        for p in &st.profiles {
            assert!(p.validate().is_ok(), "{} should still validate", p.name);
        }
    }

    #[test]
    fn old_profiles_roundtrip_keeps_legacy_fields() {
        let st: ProfileStore = serde_json::from_str(OLD_PROFILES).unwrap();
        let v = serde_json::to_value(&st).unwrap();
        let first = &v["profiles"][0];
        // Everything an older build reads is still written with the same meaning.
        assert_eq!(first["scheme"], "http");
        assert_eq!(first["mode"], "direct");
        assert_eq!(first["port"], 8742);
        assert_eq!(first["host"], "100.64.0.5");
        assert_eq!(first["transport"], "http");
        assert_eq!(first["snp_port"], 8744);
        assert_eq!(v["profiles"][2]["transport"], "https");
        // And the re-serialized form loads to the same profiles.
        let again: ProfileStore = serde_json::from_value(v).unwrap();
        assert_eq!(again.profiles, st.profiles);
    }

    #[test]
    fn explicit_transport_wins_over_scheme_and_normalizes() {
        let p: Profile = serde_json::from_str(
            r#"{"id":"x","name":"n","host":"h","port":8742,"scheme":"http","mode":"direct",
                "transport":"snp","snp_port":9000,"snp_pin":"  SHA256:AABBCCDDEEFF00112233445566778899AABBCCDDEEFF00112233445566778899 "}"#,
        )
        .unwrap();
        assert_eq!(p.transport, Transport::Snp);
        assert_eq!(p.active_port(), 9000);
        let mut p = p;
        p.normalize();
        assert!(p.snp_pin.as_deref().unwrap().starts_with("sha256:aabb"));
        assert!(p.validate().is_ok());
        p.snp_pin = Some("sha256:123".into());
        assert!(p.validate().is_err());
        p.snp_pin = None;
        p.snp_port = 0;
        assert!(p.validate().is_err());
    }

    #[test]
    fn tunnel_is_independent_of_transport() {
        let mut p = profile();
        p.mode = ConnectionMode::Ssh;
        p.ssh = Some(SshOptions { user: "me".into(), host: "box".into(), port: 22, key_path: None });
        for t in [Transport::Snp, Transport::Http, Transport::Https] {
            p.transport = t;
            p.normalize();
            assert!(p.validate().is_ok(), "{t:?} over ssh");
        }
        p.transport = Transport::Snp;
        assert_eq!(p.active_port(), 8744);
    }

    #[test]
    fn validates_hosts() {
        assert!(profile().validate().is_ok());
        let mut p = profile();
        p.host = "http://x".into();
        assert!(p.validate().is_err());
        p.host = "ai-box.tailnet.ts.net".into();
        assert!(p.validate().is_ok());
        p.mode = ConnectionMode::Ssh;
        assert!(p.validate().is_err());
        p.ssh = Some(SshOptions { user: "-oProxyCommand=x".into(), host: "h".into(), port: 22, key_path: None });
        assert!(p.validate().is_err());
    }

    #[test]
    fn formats_ipv6() {
        let mut p = profile();
        p.host = "fd7a:115c::1".into();
        assert_eq!(p.direct_url(), "http://[fd7a:115c::1]:8742");
    }
}
