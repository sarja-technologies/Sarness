//! SNP/1 - Sarness Native Protocol client (see docs/SNP.md).
//!
//! * [`cbor`]   strict SNP-CBOR codec (§4.2)
//! * [`frame`]  framing (§4)
//! * [`tls`]    TLS 1.3, certificate pinning / CA verification, trust-on-first-use probe (§5.1)
//! * [`client`] multiplexed client: handshake, flow control, cancellation, reconnect, idempotency
//! * [`adapter`] loopback adapter used by the proxy: HTTP/JSON requests → SNP operations
//!
//! There is no HTTP fallback anywhere in this module: when an SNP connection cannot be
//! established or authenticated the error is reported, never retried over another transport.

pub mod adapter;
pub mod cbor;
pub mod client;
pub mod error;
pub mod frame;
pub mod tls;

#[cfg(test)]
mod tests;

pub use client::{CallOpts, Retry, SessionInfo, SnpClient, SnpConfig};
pub use error::SnpError;
pub use tls::Pin;

use serde::Serialize;
use std::time::{Duration, Instant};

#[derive(Debug, Serialize)]
pub struct ProbeResult {
    /// `sha256:<hex>` - what gets stored in the profile once the user confirms.
    pub pin: String,
    /// Upper-case colon-separated form for display.
    pub fingerprint: String,
}

/// Trust-on-first-use: connects once, only to read the presented certificate's pin.
/// Sends no AUTH and no application data.
pub async fn probe(host: &str, port: u16, server_name: Option<&str>) -> Result<ProbeResult, SnpError> {
    let ep = tls::Endpoint { host: host.to_string(), port, server_name: server_name.map(String::from) };
    let pin = tls::probe_pin(&ep, Duration::from_secs(6)).await?;
    Ok(ProbeResult { pin: pin.to_string(), fingerprint: pin.fingerprint() })
}

pub struct TestOutcome {
    pub info: SessionInfo,
    pub health: Option<serde_json::Value>,
    pub system: Option<serde_json::Value>,
    /// Round trip of `system.health`.
    pub rtt_ms: u128,
}

/// Negotiates a session and reads the engine's health/system info. Used by "Test connection".
/// The connection is closed afterwards.
pub async fn check(cfg: SnpConfig) -> Result<TestOutcome, SnpError> {
    let client = SnpClient::new(cfg);
    let res = async {
        let info = client.connect().await?;
        let t0 = Instant::now();
        let health = client.call("system.health", cbor::Value::Map(vec![]), CallOpts::new(Retry::Safe)).await?;
        let rtt_ms = t0.elapsed().as_millis();
        let system = client.call("system.info", cbor::Value::Map(vec![]), CallOpts::new(Retry::Safe)).await.ok();
        Ok(TestOutcome {
            info: (*info).clone(),
            health: Some(cbor::to_json(&health)),
            system: system.as_ref().map(cbor::to_json),
            rtt_ms,
        })
    }
    .await;
    client.close().await;
    res
}
