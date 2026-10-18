//! Typed SNP errors. Every variant renders a message that is safe to show to the
//! user (it never contains tokens, proofs or nonces).

use super::cbor::Value;
use super::tls::Pin;
use std::fmt;
use std::time::Duration;

/// An `ERROR` frame from the server (SNP.md §6.3).
#[derive(Debug, Clone, PartialEq)]
pub struct RemoteError {
    pub code: String,
    pub status: u16,
    pub message: String,
    pub retryable: bool,
    pub detail: Option<Value>,
}

impl RemoteError {
    pub fn from_value(v: &Value) -> RemoteError {
        let code = v.get("code").and_then(Value::as_str).unwrap_or("internal").to_string();
        let status = v
            .get("status")
            .and_then(Value::as_u64)
            .and_then(|s| u16::try_from(s).ok())
            .filter(|s| (100..=599).contains(s))
            .unwrap_or_else(|| default_status(&code));
        RemoteError {
            message: v.get("message").and_then(Value::as_str).unwrap_or("request failed").to_string(),
            retryable: v.get("retryable").and_then(Value::as_bool).unwrap_or(false),
            detail: v.get("detail").cloned().filter(|d| !d.is_null()),
            code,
            status,
        }
    }
}

fn default_status(code: &str) -> u16 {
    match code {
        "bad_request" => 400,
        "unauthenticated" => 401,
        "not_found" | "unknown_op" => 404,
        "conflict" => 409,
        "too_large" => 413,
        "rate_limited" => 429,
        "cancelled" => 499,
        "resource_exhausted" | "unavailable" => 503,
        "deadline_exceeded" | "stalled" => 504,
        _ => 500,
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum SnpError {
    Dns { host: String, detail: String },
    Refused { addr: String },
    Timeout { stage: String },
    Io(String),
    Tls(String),
    PinMismatch { expected: Pin, presented: Option<Pin> },
    UntrustedCertificate { detail: String, presented: Option<Pin> },
    NoServerIdentity(String),
    Alpn,
    UnsupportedVersion { supported: Vec<u8> },
    Unauthenticated(String),
    RateLimited(String),
    ServerProof,
    Protocol(String),
    Limit(String),
    GoAway { code: String, message: String },
    /// The server sent GOAWAY and never processed this stream: safe to retry.
    NotProcessed,
    Remote(RemoteError),
    Cancelled,
    /// The connection died (idle timeout, reset, EOF, …).
    ConnectionLost(String),
    DeadlineExceeded,
    StreamClosed,
    /// Reconnect attempts are rate-limited by exponential back-off.
    Backoff { retry_in: Duration, last: Box<SnpError> },
}

impl SnpError {
    /// Errors after which the client must not reconnect on its own (SNP.md §10) and
    /// must never try another transport. The profile has to be changed or the user
    /// must reconnect explicitly.
    pub fn is_fatal(&self) -> bool {
        match self {
            SnpError::PinMismatch { .. }
            | SnpError::UntrustedCertificate { .. }
            | SnpError::NoServerIdentity(_)
            | SnpError::Alpn
            | SnpError::UnsupportedVersion { .. }
            | SnpError::Unauthenticated(_)
            | SnpError::ServerProof => true,
            SnpError::Backoff { last, .. } => last.is_fatal(),
            _ => false,
        }
    }

    /// Security or authentication failure (as opposed to plain unreachability).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn is_security(&self) -> bool {
        matches!(
            self,
            SnpError::PinMismatch { .. }
                | SnpError::UntrustedCertificate { .. }
                | SnpError::NoServerIdentity(_)
                | SnpError::Unauthenticated(_)
                | SnpError::ServerProof
        )
    }

    /// Stable machine-readable category for the UI.
    pub fn kind(&self) -> &'static str {
        match self {
            SnpError::Dns { .. } => "dns",
            SnpError::Refused { .. } => "refused",
            SnpError::Timeout { .. } => "timeout",
            SnpError::Io(_) | SnpError::ConnectionLost(_) | SnpError::StreamClosed => "connection",
            SnpError::Tls(_) | SnpError::Alpn => "tls",
            SnpError::PinMismatch { .. } => "pin_mismatch",
            SnpError::UntrustedCertificate { .. } | SnpError::NoServerIdentity(_) => "untrusted_certificate",
            SnpError::UnsupportedVersion { .. } => "version",
            SnpError::Unauthenticated(_) => "auth",
            SnpError::RateLimited(_) => "rate_limited",
            SnpError::ServerProof => "server_proof",
            SnpError::Protocol(_) => "protocol",
            SnpError::Limit(_) => "limit",
            SnpError::GoAway { .. } | SnpError::NotProcessed => "goaway",
            SnpError::Remote(_) => "remote",
            SnpError::Cancelled => "cancelled",
            SnpError::DeadlineExceeded => "deadline",
            SnpError::Backoff { last, .. } => last.kind(),
        }
    }

    /// Connection stage reported by "Test connection".
    pub fn stage(&self) -> &'static str {
        match self {
            SnpError::Dns { .. } | SnpError::Refused { .. } | SnpError::Io(_) | SnpError::ConnectionLost(_) => "connect",
            SnpError::Timeout { .. } => "connect",
            SnpError::Tls(_) | SnpError::Alpn => "tls",
            SnpError::PinMismatch { .. } | SnpError::UntrustedCertificate { .. } | SnpError::NoServerIdentity(_) => "identity",
            SnpError::UnsupportedVersion { .. } => "version",
            SnpError::Unauthenticated(_) | SnpError::RateLimited(_) => "auth",
            SnpError::ServerProof => "server_proof",
            SnpError::Backoff { last, .. } => last.stage(),
            _ => "protocol",
        }
    }

    /// Whether the operation may be re-sent on a fresh connection (before considering idempotency).
    pub fn is_retryable_transport(&self) -> bool {
        match self {
            SnpError::ConnectionLost(_) | SnpError::Io(_) | SnpError::NotProcessed => true,
            SnpError::GoAway { code, .. } => matches!(code.as_str(), "shutdown" | "idle_timeout" | "resource_exhausted" | "normal"),
            SnpError::Remote(r) => r.retryable && matches!(r.code.as_str(), "rate_limited" | "resource_exhausted" | "unavailable" | "stalled"),
            _ => false,
        }
    }

    /// HTTP-compatible status used by the loopback adapter.
    pub fn http_status(&self) -> u16 {
        match self {
            SnpError::Remote(r) => r.status,
            SnpError::Unauthenticated(_) => 401,
            SnpError::Cancelled => 499,
            SnpError::DeadlineExceeded => 504,
            SnpError::Limit(_) => 413,
            SnpError::Backoff { last, .. } => last.http_status(),
            _ => 502,
        }
    }
}

fn pin_or_unknown(p: &Option<Pin>) -> String {
    p.map(|p| p.to_string()).unwrap_or_else(|| "(unknown)".into())
}

impl fmt::Display for SnpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SnpError::Dns { host, detail } => write!(f, "Could not resolve \"{host}\" ({detail}). Check the server address."),
            SnpError::Refused { addr } => write!(f, "Connection to {addr} was refused. Check the SNP port and that the Sarness Engine is running with SNP enabled."),
            SnpError::Timeout { stage } => write!(f, "Timed out while waiting for the server ({stage}). Check the address, port and network connection."),
            SnpError::Io(e) => write!(f, "Network error: {e}"),
            SnpError::Tls(e) => write!(f, "Secure connection failed: {e}"),
            SnpError::PinMismatch { expected, presented } => write!(
                f,
                "The server's identity does not match the saved one. Expected {expected} but the server presented {}. \
                 If you did not re-key or reinstall the server, do not continue - someone may be impersonating it.",
                pin_or_unknown(presented)
            ),
            SnpError::UntrustedCertificate { detail, presented } => write!(
                f,
                "The server's certificate is not trusted ({detail}). Server identity: {}. \
                 Use \"Fetch identity\" to review and pin it, or install a certificate from a trusted authority.",
                pin_or_unknown(presented)
            ),
            SnpError::NoServerIdentity(m) => write!(f, "{m}"),
            SnpError::Alpn => write!(f, "The server did not negotiate the SNP protocol (ALPN snp/1). Is this the SNP port?"),
            SnpError::UnsupportedVersion { supported } => write!(
                f,
                "No common SNP version: this app speaks version 1, the server supports {supported:?}. Update Sarness on one side."
            ),
            SnpError::Unauthenticated(m) => write!(f, "The server rejected the API token{}. Copy the token from the server again.", if m.is_empty() { String::new() } else { format!(" ({m})") }),
            SnpError::RateLimited(m) => write!(f, "The server is rate limiting this client{}. Wait a minute and try again.", if m.is_empty() { String::new() } else { format!(" ({m})") }),
            SnpError::ServerProof => write!(
                f,
                "The server did not prove that it knows the API token (bad server proof). The connection was closed; \
                 this can mean a different server or an impostor."
            ),
            SnpError::Protocol(m) => write!(f, "Protocol error: {m}"),
            SnpError::Limit(m) => write!(f, "Request exceeds a protocol limit: {m}"),
            SnpError::GoAway { code, message } => write!(f, "The server closed the connection ({code}{}).", if message.is_empty() { String::new() } else { format!(": {message}") }),
            SnpError::NotProcessed => write!(f, "The server was shutting down and did not process the request."),
            SnpError::Remote(r) => write!(f, "{}", r.message),
            SnpError::Cancelled => write!(f, "The request was cancelled."),
            SnpError::ConnectionLost(m) => write!(f, "The connection to the server was lost ({m})."),
            SnpError::DeadlineExceeded => write!(f, "The request took too long."),
            SnpError::StreamClosed => write!(f, "The stream is closed."),
            SnpError::Backoff { retry_in, last } => write!(f, "{last} (retrying in {} s)", retry_in.as_secs().max(1)),
        }
    }
}

impl std::error::Error for SnpError {}

impl From<std::io::Error> for SnpError {
    fn from(e: std::io::Error) -> Self {
        SnpError::Io(e.to_string())
    }
}
