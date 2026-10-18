//! TLS 1.3 transport and server identity (SNP.md §3, §5.1).
//!
//! There is deliberately no "insecure" mode. A connection is verified either
//!   * against a pinned SHA-256 of the certificate's SubjectPublicKeyInfo (the
//!     `CertificateVerify` signature is still checked by the crypto provider), or
//!   * against the system trust store with hostname verification.
//! The only other verifier, [`Trust::Probe`], exists for trust-on-first-use: it is
//! used exclusively by [`probe_pin`], which closes the connection right after the
//! handshake and never exposes the stream, so no `AUTH` can ever be sent over it.

use super::error::SnpError;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::{Resumption, WebPkiServerVerifier};
use rustls::crypto::{verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{CertificateError, DigitallySignedStruct, SignatureScheme};
use sha2::{Digest, Sha256};
use std::fmt;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;

pub const ALPN: &[u8] = b"snp/1";

/// `sha256:<64 lowercase hex>` of the DER SubjectPublicKeyInfo.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct Pin(pub [u8; 32]);

impl Pin {
    pub fn parse(s: &str) -> Result<Pin, String> {
        let hexpart = s.trim().strip_prefix("sha256:").ok_or("a pin must look like sha256:<64 hex digits>")?;
        let bytes = hex::decode(hexpart).map_err(|_| "a pin must contain only hexadecimal digits".to_string())?;
        let arr: [u8; 32] = bytes.try_into().map_err(|_| "a pin must be exactly 32 bytes (64 hex digits)".to_string())?;
        Ok(Pin(arr))
    }

    /// Colon-separated upper-case form for display, e.g. `AB:CD:…`.
    pub fn fingerprint(&self) -> String {
        self.0.iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(":")
    }
}

impl fmt::Display for Pin {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "sha256:{}", hex::encode(self.0))
    }
}

impl fmt::Debug for Pin {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Pin({self})")
    }
}

pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

// ------------------------------------------------------------------- X.509 SPKI
/// Reads one DER TLV at the start of `b`: (tag, header_len, content_len).
fn der_tlv(b: &[u8]) -> Result<(u8, usize, usize), String> {
    if b.len() < 2 {
        return Err("truncated DER".into());
    }
    let tag = b[0];
    let l0 = b[1];
    let (hdr, len) = if l0 < 0x80 {
        (2usize, l0 as usize)
    } else {
        let n = (l0 & 0x7f) as usize;
        if n == 0 || n > 4 || b.len() < 2 + n {
            return Err("unsupported DER length".into());
        }
        let mut len = 0usize;
        for &x in &b[2..2 + n] {
            len = (len << 8) | x as usize;
        }
        (2 + n, len)
    };
    if hdr.checked_add(len).map(|t| t > b.len()).unwrap_or(true) {
        return Err("DER length exceeds input".into());
    }
    Ok((tag, hdr, len))
}

/// SHA-256 of the DER-encoded SubjectPublicKeyInfo of an X.509 certificate.
pub fn spki_sha256(cert_der: &[u8]) -> Result<[u8; 32], String> {
    // Certificate ::= SEQUENCE { tbsCertificate SEQUENCE { … }, signatureAlgorithm, signature }
    let (tag, hdr, len) = der_tlv(cert_der)?;
    if tag != 0x30 {
        return Err("certificate is not a DER SEQUENCE".into());
    }
    let cert = &cert_der[hdr..hdr + len];
    let (tag, hdr, len) = der_tlv(cert)?;
    if tag != 0x30 {
        return Err("tbsCertificate is not a SEQUENCE".into());
    }
    let mut tbs = &cert[hdr..hdr + len];
    // [0] EXPLICIT version (optional), serial, signature, issuer, validity, subject.
    let (tag, h, l) = der_tlv(tbs)?;
    if tag == 0xA0 {
        tbs = &tbs[h + l..];
    }
    for expected in [0x02u8, 0x30, 0x30, 0x30, 0x30] {
        let (tag, h, l) = der_tlv(tbs)?;
        if tag != expected {
            return Err("unexpected tbsCertificate layout".into());
        }
        tbs = &tbs[h + l..];
    }
    let (tag, h, l) = der_tlv(tbs)?;
    if tag != 0x30 {
        return Err("subjectPublicKeyInfo is not a SEQUENCE".into());
    }
    Ok(Sha256::digest(&tbs[..h + l]).into())
}

// -------------------------------------------------------------------- verifier
#[derive(Clone, Debug)]
pub enum Trust {
    /// Certificate SPKI must match this pin.
    Pinned(Pin),
    /// System trust store + hostname (`server_name` overrides the connect host).
    System,
    /// Trust-on-first-use probe only: accepts any certificate, records its pin.
    Probe,
}

/// Records the pin of the certificate the server actually presented.
#[derive(Clone, Default)]
pub struct PresentedPin(Arc<Mutex<Option<Pin>>>);

impl PresentedPin {
    pub fn get(&self) -> Option<Pin> {
        *self.0.lock().unwrap()
    }
    fn set(&self, p: Pin) {
        *self.0.lock().unwrap() = Some(p);
    }
}

struct SnpVerifier {
    trust: Trust,
    provider: Arc<CryptoProvider>,
    webpki: Option<Arc<WebPkiServerVerifier>>,
    presented: PresentedPin,
}

impl fmt::Debug for SnpVerifier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "SnpVerifier({:?})", self.trust)
    }
}

impl ServerCertVerifier for SnpVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let spki = spki_sha256(end_entity.as_ref())
            .map_err(|_| rustls::Error::InvalidCertificate(CertificateError::BadEncoding))?;
        self.presented.set(Pin(spki));
        match &self.trust {
            Trust::Pinned(p) => {
                if constant_time_eq(&p.0, &spki) {
                    Ok(ServerCertVerified::assertion())
                } else {
                    Err(rustls::Error::InvalidCertificate(CertificateError::ApplicationVerificationFailure))
                }
            }
            Trust::Probe => Ok(ServerCertVerified::assertion()),
            Trust::System => self
                .webpki
                .as_ref()
                .ok_or_else(|| rustls::Error::General("no system trust anchors".into()))?
                .verify_server_cert(end_entity, intermediates, server_name, ocsp_response, now),
        }
    }

    fn verify_tls12_signature(&self, _m: &[u8], _c: &CertificateDer<'_>, _d: &DigitallySignedStruct) -> Result<HandshakeSignatureValid, rustls::Error> {
        // SNP is TLS 1.3 only; the protocol-version list already excludes 1.2.
        Err(rustls::Error::PeerIncompatible(rustls::PeerIncompatible::Tls12NotOffered))
    }

    fn verify_tls13_signature(&self, message: &[u8], cert: &CertificateDer<'_>, dss: &DigitallySignedStruct) -> Result<HandshakeSignatureValid, rustls::Error> {
        // Even for pinned identities the CertificateVerify signature must prove possession of the key.
        verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

fn system_roots() -> Result<rustls::RootCertStore, SnpError> {
    let mut roots = rustls::RootCertStore::empty();
    let res = rustls_native_certs::load_native_certs();
    for c in res.certs {
        let _ = roots.add(c);
    }
    if roots.is_empty() {
        return Err(SnpError::NoServerIdentity(
            "no system certificate authorities are available; pin the server identity instead".into(),
        ));
    }
    Ok(roots)
}

pub fn client_config(trust: &Trust) -> Result<(Arc<rustls::ClientConfig>, PresentedPin), SnpError> {
    let provider = provider();
    let presented = PresentedPin::default();
    let webpki = match trust {
        Trust::System => Some(
            WebPkiServerVerifier::builder_with_provider(Arc::new(system_roots()?), provider.clone())
                .build()
                .map_err(|e| SnpError::NoServerIdentity(format!("cannot build certificate verifier: {e}")))?,
        ),
        _ => None,
    };
    let verifier = Arc::new(SnpVerifier { trust: trust.clone(), provider: provider.clone(), webpki, presented: presented.clone() });
    let mut cfg = rustls::ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|e| SnpError::Tls(e.to_string()))?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    cfg.alpn_protocols = vec![ALPN.to_vec()];
    // Every connection is verified from scratch; no session tickets.
    cfg.resumption = Resumption::disabled();
    Ok((Arc::new(cfg), presented))
}

pub fn server_name_for(host: &str, override_name: Option<&str>) -> Result<ServerName<'static>, SnpError> {
    let n = override_name.filter(|n| !n.trim().is_empty()).unwrap_or(host);
    let n = n.trim().trim_start_matches('[').trim_end_matches(']');
    ServerName::try_from(n.to_string()).map_err(|_| SnpError::Tls(format!("\"{n}\" is not a valid server name")))
}

fn map_io_tls_error(e: std::io::Error, trust: &Trust, presented: &PresentedPin) -> SnpError {
    use rustls::Error as R;
    if let Some(re) = e.get_ref().and_then(|i| i.downcast_ref::<R>()) {
        return match re {
            R::InvalidCertificate(CertificateError::ApplicationVerificationFailure) => match trust {
                Trust::Pinned(expected) => SnpError::PinMismatch { expected: *expected, presented: presented.get() },
                _ => SnpError::Tls(re.to_string()),
            },
            R::InvalidCertificate(c) => SnpError::UntrustedCertificate { detail: format!("{c:?}"), presented: presented.get() },
            R::AlertReceived(rustls::AlertDescription::ProtocolVersion) | R::PeerIncompatible(_) => {
                SnpError::Tls("the server does not offer TLS 1.3, which SNP requires".into())
            }
            R::AlertReceived(rustls::AlertDescription::NoApplicationProtocol) => SnpError::Alpn,
            other => SnpError::Tls(other.to_string()),
        };
    }
    match e.kind() {
        std::io::ErrorKind::UnexpectedEof | std::io::ErrorKind::ConnectionReset => SnpError::Tls(
            "the server closed the connection during the TLS handshake (is this an SNP port?)".into(),
        ),
        _ => SnpError::Io(e.to_string()),
    }
}

/// Where to connect.
#[derive(Clone, Debug)]
pub struct Endpoint {
    pub host: String,
    pub port: u16,
    /// Name used for SNI and CA hostname verification (defaults to `host`).
    pub server_name: Option<String>,
}

pub type TlsStream = tokio_rustls::client::TlsStream<TcpStream>;

async fn tcp_connect(ep: &Endpoint, timeout: Duration) -> Result<TcpStream, SnpError> {
    let host = ep.host.trim().trim_start_matches('[').trim_end_matches(']').to_string();
    let addrs: Vec<SocketAddr> = match tokio::time::timeout(timeout, tokio::net::lookup_host((host.as_str(), ep.port))).await {
        Err(_) => return Err(SnpError::Timeout { stage: "dns".into() }),
        Ok(Err(e)) => return Err(SnpError::Dns { host: host.clone(), detail: e.to_string() }),
        Ok(Ok(it)) => it.collect(),
    };
    if addrs.is_empty() {
        return Err(SnpError::Dns { host, detail: "no addresses".into() });
    }
    let mut last: Option<SnpError> = None;
    for a in addrs {
        match tokio::time::timeout(timeout, TcpStream::connect(a)).await {
            Err(_) => last = Some(SnpError::Timeout { stage: "connect".into() }),
            Ok(Err(e)) => {
                last = Some(if e.kind() == std::io::ErrorKind::ConnectionRefused {
                    SnpError::Refused { addr: a.to_string() }
                } else {
                    SnpError::Io(format!("{a}: {e}"))
                })
            }
            Ok(Ok(s)) => {
                let _ = s.set_nodelay(true);
                let ka = socket2::TcpKeepalive::new().with_time(Duration::from_secs(30));
                let _ = socket2::SockRef::from(&s).set_tcp_keepalive(&ka);
                return Ok(s);
            }
        }
    }
    Err(last.unwrap_or(SnpError::Io("connect failed".into())))
}

/// TCP + TLS 1.3 with ALPN `snp/1`, verified per `trust`. Returns the stream and
/// the pin of the certificate actually presented.
pub async fn connect(ep: &Endpoint, trust: &Trust, timeout: Duration) -> Result<(TlsStream, Pin), SnpError> {
    if matches!(trust, Trust::Probe) {
        // A probe-verified stream must never be used for a session.
        return Err(SnpError::NoServerIdentity("a server identity (pin or CA) is required".into()));
    }
    connect_any(ep, trust, timeout).await
}

async fn connect_any(ep: &Endpoint, trust: &Trust, timeout: Duration) -> Result<(TlsStream, Pin), SnpError> {
    let (cfg, presented) = client_config(trust)?;
    let name = server_name_for(&ep.host, ep.server_name.as_deref())?;
    let tcp = tcp_connect(ep, timeout).await?;
    let connector = TlsConnector::from(cfg);
    let tls = match tokio::time::timeout(timeout, connector.connect(name, tcp)).await {
        Err(_) => return Err(SnpError::Timeout { stage: "tls".into() }),
        Ok(Err(e)) => return Err(map_io_tls_error(e, trust, &presented)),
        Ok(Ok(s)) => s,
    };
    if tls.get_ref().1.alpn_protocol() != Some(ALPN) {
        return Err(SnpError::Alpn);
    }
    let pin = presented.get().ok_or_else(|| SnpError::Tls("server presented no certificate".into()))?;
    Ok((tls, pin))
}

/// Trust-on-first-use: connects only to read the certificate the server presents
/// and returns its pin. Never sends any application data (in particular no AUTH).
pub async fn probe_pin(ep: &Endpoint, timeout: Duration) -> Result<Pin, SnpError> {
    let (mut tls, pin) = connect_any(ep, &Trust::Probe, timeout).await?;
    use tokio::io::AsyncWriteExt;
    let _ = tokio::time::timeout(Duration::from_secs(2), tls.shutdown()).await;
    Ok(pin)
}
