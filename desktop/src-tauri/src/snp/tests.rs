//! SNP/1 conformance tests for the desktop client (SNP.md §12).
//!
//! The fake server in `fake` is written independently of the client: it has its own
//! frame reader/writer, its own flow-control bookkeeping, and checks the client's
//! AUTH proof with the `hmac` crate directly.

use super::cbor::{self, CborError, Value};
use super::client::{client_proof, server_proof, token_key};
use super::frame::{self, ftype, Frame, FrameError, PRE_AUTH_MAX};
use bytes::BytesMut;

fn hx(s: &str) -> Vec<u8> {
    hex::decode(s.replace(' ', "")).unwrap()
}

// ------------------------------------------------------------------ §12.2 vectors
#[test]
fn spec_auth_vector() {
    let k = token_key("sk-test");
    assert_eq!(hex::encode(k), "f3abf2a6cc4f00987743db5f544ba345b4899ae31f326d8ee9c4816de153c9e0");
    let proof = client_proof(&k, &[0u8; 32], &[0x11u8; 16], &[0x22u8; 32]);
    assert_eq!(hex::encode(proof), "5b2fc2282c3e222fcbeb00e7b86d643140edad01fbe84ed8c8b473418c797b3f");
    // The server proof uses a different label and operand order, so it must differ.
    assert_ne!(server_proof(&k, &[0x11u8; 16], &[0u8; 32], &[0x22u8; 32]), proof);
}

#[test]
fn spec_ping_frame_vector() {
    let bytes = hx("53 4E 05 00 00 00 00 00 00 00 00 08 01 02 03 04 05 06 07 08");
    let mut buf = BytesMut::from(&bytes[..]);
    let f = frame::try_parse(&mut buf, PRE_AUTH_MAX).unwrap().unwrap();
    assert_eq!((f.ftype, f.stream), (ftype::PING, 0));
    assert_eq!(&f.payload[..], &[1, 2, 3, 4, 5, 6, 7, 8]);
    assert!(buf.is_empty());
    assert_eq!(f.encode(), bytes);
}

// ------------------------------------------------------------------ framing
#[test]
fn frame_header_rejections() {
    let max = 1024;
    let hdr = |magic: [u8; 2], t: u8, flags: u8, sid: u32, len: u32| {
        let mut h = vec![magic[0], magic[1], t, flags];
        h.extend_from_slice(&sid.to_be_bytes());
        h.extend_from_slice(&len.to_be_bytes());
        h
    };
    assert!(matches!(frame::parse_header(&hdr([0x53, 0x4F], 5, 0, 0, 0), max), Err(FrameError::BadMagic(..))));
    assert!(matches!(frame::parse_header(&hdr([0x53, 0x4E], 5, 1, 0, 0), max), Err(FrameError::BadFlags(1))));
    assert!(matches!(frame::parse_header(&hdr([0x53, 0x4E], 0x99, 0, 0, 0), max), Err(FrameError::UnknownType(0x99))));
    assert!(matches!(frame::parse_header(&hdr([0x53, 0x4E], 0x00, 0, 0, 0), max), Err(FrameError::UnknownType(0))));
    assert!(matches!(frame::parse_header(&hdr([0x53, 0x4E], 0x11, 0, 1, 1025), max), Err(FrameError::TooLarge { len: 1025, max: 1024 })));
    // A hostile length is rejected from the header alone, without the payload being present.
    let mut buf = BytesMut::from(&hdr([0x53, 0x4E], 0x12, 0, 1, u32::MAX)[..]);
    assert!(matches!(frame::try_parse(&mut buf, PRE_AUTH_MAX), Err(FrameError::TooLarge { .. })));
    assert!(frame::parse_header(&hdr([0x53, 0x4E], 0x11, 0, 1, 1024), max).is_ok());
}

#[test]
fn frame_partial_input_waits() {
    let f = Frame::new(ftype::MSG, 7, vec![0xa0u8]);
    let enc = f.encode();
    for cut in 0..enc.len() {
        let mut b = BytesMut::from(&enc[..cut]);
        assert!(frame::try_parse(&mut b, 1024).unwrap().is_none(), "cut {cut}");
    }
    let mut b = BytesMut::from(&enc[..]);
    assert_eq!(frame::try_parse(&mut b, 1024).unwrap().unwrap(), f);
}

#[tokio::test]
async fn frame_reader_reports_truncation_and_clean_close() {
    let enc = Frame::new(ftype::MSG, 1, vec![0xa0u8]).encode();
    let (mut a, b) = tokio::io::duplex(64);
    use tokio::io::AsyncWriteExt;
    a.write_all(&enc[..enc.len() - 1]).await.unwrap();
    drop(a);
    let mut r = frame::FrameReader::new(b, 1024);
    assert!(matches!(r.next().await, Err(FrameError::Truncated)));
    let (a, b) = tokio::io::duplex(64);
    drop(a);
    let mut r = frame::FrameReader::new(b, 1024);
    assert!(matches!(r.next().await, Err(FrameError::Closed)));
}

// -------------------------------------------------------------------- CBOR
#[test]
fn cbor_roundtrip_and_deterministic_order() {
    let v = Value::map([
        ("zebra", Value::Int(-1)),
        ("a", Value::Array(vec![Value::Null, Value::Bool(true), Value::Float(1.5), Value::Bytes(vec![1, 2, 3]), Value::text("héllo")])),
        ("bb", Value::Int(i64::MAX)),
        ("min", Value::Int(i64::MIN)),
    ]);
    let enc = cbor::encode(&v).unwrap();
    // keys sorted by encoded bytes: "a" < "bb" < "min" < "zebra" (length first, then bytes)
    let dec = cbor::decode(&enc).unwrap();
    let keys: Vec<&str> = match &dec {
        Value::Map(m) => m.iter().map(|(k, _)| k.as_str()).collect(),
        _ => panic!(),
    };
    assert_eq!(keys, ["a", "bb", "min", "zebra"]);
    assert_eq!(cbor::encode(&dec).unwrap(), enc);
    assert_eq!(dec.get("min"), Some(&Value::Int(i64::MIN)));
    assert_eq!(dec.get("bb"), Some(&Value::Int(i64::MAX)));
}

#[test]
fn cbor_shortest_integers_and_floats() {
    assert_eq!(cbor::encode(&Value::Int(23)).unwrap(), hx("17"));
    assert_eq!(cbor::encode(&Value::Int(24)).unwrap(), hx("18 18"));
    assert_eq!(cbor::encode(&Value::Int(255)).unwrap(), hx("18 ff"));
    assert_eq!(cbor::encode(&Value::Int(256)).unwrap(), hx("19 01 00"));
    assert_eq!(cbor::encode(&Value::Int(65536)).unwrap(), hx("1a 00 01 00 00"));
    assert_eq!(cbor::encode(&Value::Int(1 << 32)).unwrap(), hx("1b 00 00 00 01 00 00 00 00"));
    assert_eq!(cbor::encode(&Value::Int(-1)).unwrap(), hx("20"));
    assert_eq!(cbor::encode(&Value::Int(-25)).unwrap(), hx("38 18"));
    assert_eq!(cbor::encode(&Value::Float(1.5)).unwrap(), hx("f9 3e 00"));
    assert_eq!(cbor::encode(&Value::Float(0.0)).unwrap(), hx("f9 00 00"));
    assert_eq!(cbor::encode(&Value::Float(100000.0)).unwrap(), hx("fa 47 c3 50 00"));
    assert_eq!(cbor::encode(&Value::Float(1.1)).unwrap(), hx("fb 3f f1 99 99 99 99 99 9a"));
    assert_eq!(cbor::encode(&Value::Float(5.960464477539063e-8)).unwrap(), hx("f9 00 01"));
    for f in [0.1, -2.5, 65504.0, 6.1e-5, 1e300, 3.4e38, 1e-300] {
        let b = cbor::encode(&Value::Float(f)).unwrap();
        assert_eq!(cbor::decode(&b).unwrap(), Value::Float(f), "{f}");
    }
    assert_eq!(cbor::encode(&Value::Float(f64::NAN)), Err(CborError::NonFinite));
    assert_eq!(cbor::encode(&Value::Float(f64::INFINITY)), Err(CborError::NonFinite));
}

#[test]
fn cbor_rejection_vectors() {
    let bad: &[(&str, &str, CborError)] = &[
        ("tag", "c0 00", CborError::Tag),
        ("undefined", "f7", CborError::Simple),
        ("simple(16)", "f0", CborError::Simple),
        ("simple one-byte", "f8 20", CborError::Simple),
        ("indefinite bytes", "5f 41 00 ff", CborError::Indefinite),
        ("indefinite text", "7f 61 61 ff", CborError::Indefinite),
        ("indefinite array", "9f 00 ff", CborError::Indefinite),
        ("indefinite map", "bf 61 61 00 ff", CborError::Indefinite),
        ("break alone", "ff", CborError::Indefinite),
        ("reserved ai 28", "1c", CborError::Reserved),
        ("reserved ai 30 (array)", "9e", CborError::Reserved),
        ("non-shortest 1-byte", "18 17", CborError::NonShortest),
        ("non-shortest 2-byte", "19 00 ff", CborError::NonShortest),
        ("non-shortest 4-byte", "1a 00 00 ff ff", CborError::NonShortest),
        ("non-shortest 8-byte", "1b 00 00 00 00 ff ff ff ff", CborError::NonShortest),
        ("non-shortest negative", "38 01", CborError::NonShortest),
        ("non-shortest text length", "78 01 61", CborError::NonShortest),
        ("uint > i64", "1b 80 00 00 00 00 00 00 00", CborError::IntRange),
        ("uint max", "1b ff ff ff ff ff ff ff ff", CborError::IntRange),
        ("nint < -2^63", "3b 80 00 00 00 00 00 00 00", CborError::IntRange),
        ("NaN f16", "f9 7e 00", CborError::NonFinite),
        ("inf f16", "f9 7c 00", CborError::NonFinite),
        ("-inf f32", "fa ff 80 00 00", CborError::NonFinite),
        ("NaN f64", "fb 7f f8 00 00 00 00 00 00", CborError::NonFinite),
        ("invalid utf-8", "62 c3 28", CborError::Utf8),
        ("overlong utf-8", "62 c0 af", CborError::Utf8),
        ("map int key", "a1 01 02", CborError::KeyType),
        ("map bytes key", "a1 41 61 02", CborError::KeyType),
        ("duplicate key", "a2 61 61 01 61 61 02", CborError::DuplicateKey),
        ("trailing", "00 00", CborError::Trailing),
        ("truncated head", "19 01", CborError::Truncated),
        ("empty", "", CborError::Truncated),
        ("text longer than input", "65 61 62", CborError::Length),
        ("bytes length u64::MAX", "5b ff ff ff ff ff ff ff ff", CborError::Length),
        ("array count > remaining", "84 00 00", CborError::Length),
        ("array count 2^32", "9b 00 00 00 01 00 00 00 00", CborError::Length),
        ("map count > remaining/2", "a2 61 61 00", CborError::Length),
    ];
    for (name, h, want) in bad {
        let got = cbor::decode(&hx(h)).expect_err(name);
        assert_eq!(&got, want, "{name}");
    }
}

#[test]
fn cbor_depth_and_item_limits() {
    let nested = |n: usize| -> Vec<u8> {
        let mut v = vec![0x81u8; n];
        v.push(0x00);
        v
    };
    assert!(cbor::decode(&nested(32)).is_ok());
    assert_eq!(cbor::decode(&nested(33)), Err(CborError::Depth));
    // 100 000 items fit, 100 001 do not (array header + n elements).
    let arr = |n: usize| -> Vec<u8> {
        let mut v = vec![0x9a];
        v.extend_from_slice(&(n as u32).to_be_bytes());
        v.extend(std::iter::repeat(0u8).take(n));
        v
    };
    assert!(cbor::decode(&arr(99_999)).is_ok());
    assert_eq!(cbor::decode(&arr(100_000)), Err(CborError::Items));
    // A huge declared count with a tiny payload is rejected before anything is allocated.
    assert_eq!(cbor::decode(&hx("9a ff ff ff ff")), Err(CborError::Length));
    // Encoder refuses what the decoder would refuse.
    let mut deep = Value::Null;
    for _ in 0..33 {
        deep = Value::Array(vec![deep]);
    }
    assert_eq!(cbor::encode(&deep), Err(CborError::Depth));
}

#[test]
fn cbor_accepts_any_float_width_and_text_keys_in_any_order() {
    assert_eq!(cbor::decode(&hx("f9 3c 00")).unwrap(), Value::Float(1.0));
    assert_eq!(cbor::decode(&hx("fa 3f 80 00 00")).unwrap(), Value::Float(1.0));
    assert_eq!(cbor::decode(&hx("fb 3f f0 00 00 00 00 00 00")).unwrap(), Value::Float(1.0));
    // Decoders MUST NOT require canonical key order.
    let m = cbor::decode(&hx("a2 61 62 01 61 61 02")).unwrap();
    assert_eq!(m.get("a"), Some(&Value::Int(2)));
    assert_eq!(m.get("b"), Some(&Value::Int(1)));
}

#[test]
fn json_bridge_uses_base64_for_bytes_only_at_the_edge() {
    let v = Value::map([("raw", Value::Bytes(vec![0xde, 0xad])), ("n", Value::Int(3)), ("f", Value::Float(0.5))]);
    let j = cbor::to_json(&v);
    assert_eq!(j["raw"], "3q0=");
    assert_eq!(j["n"], 3);
    let back = cbor::from_json(&serde_json::json!({"a": [1, 2.5, null, true, "x"], "big": 18446744073709551615u64}));
    assert_eq!(back.get("a").unwrap().as_array().unwrap()[1], Value::Float(2.5));
    assert!(matches!(back.get("big"), Some(Value::Float(_))));
}

// ===========================================================================
// Client against the in-process fake server
// ===========================================================================
mod fake;

use super::client::{BackoffPolicy, CallOpts, Event, Retry, SnpClient, SnpConfig, SubEvent};
use super::error::SnpError;
use super::tls::{self, Pin};
use bytes::Bytes;
use fake::{m, Knobs, Reply, Server, TOKEN};
use std::sync::atomic::Ordering::SeqCst;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn cfg(s: &Server) -> SnpConfig {
    let mut c = SnpConfig::new("127.0.0.1", s.port(), Some(Pin::parse(&s.pin()).unwrap()), TOKEN);
    c.backoff = BackoffPolicy { initial: Duration::from_millis(100), factor: 2.0, cap: Duration::from_millis(400), jitter: 0.25 };
    c.connect_timeout = Duration::from_secs(3);
    c
}

fn client(s: &Server) -> SnpClient {
    SnpClient::new(cfg(s))
}

fn args(items: Vec<(&str, Value)>) -> Value {
    m(items)
}

fn none() -> Value {
    Value::Map(vec![])
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    for _ in 0..200 {
        if f() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("timed out waiting for: {what}");
}

async fn drain_to_end(s: &mut super::client::SnpStream) -> (Vec<Value>, Vec<u8>, Value) {
    let (mut msgs, mut data) = (vec![], vec![]);
    loop {
        match tokio::time::timeout(Duration::from_secs(10), s.recv()).await.expect("stream stalled") {
            Ok(Event::Msg(v)) => msgs.push(v),
            Ok(Event::Data(b)) => data.extend_from_slice(&b),
            Ok(Event::End(v)) => return (msgs, data, v),
            Err(e) => panic!("stream failed: {e}"),
        }
    }
}

// ------------------------------------------------------------------ identity
#[test]
fn spki_extraction_matches_the_key_the_cert_was_made_from() {
    for alg in [&rcgen::PKCS_ECDSA_P256_SHA256, &rcgen::PKCS_ED25519] {
        let id = fake::identity_with(alg, false, true);
        assert_eq!(tls::spki_sha256(&id.cert_der).unwrap(), id.spki);
    }
    assert!(tls::spki_sha256(&[0x30, 0x03, 0x01, 0x01, 0x00]).is_err());
    assert!(tls::spki_sha256(b"not a certificate").is_err());
    assert!(tls::spki_sha256(&[]).is_err());
}

#[test]
fn pin_parsing_and_constant_time_compare() {
    let p = Pin::parse(&format!("sha256:{}", "ab".repeat(32))).unwrap();
    assert_eq!(p.to_string(), format!("sha256:{}", "ab".repeat(32)));
    assert!(p.fingerprint().starts_with("AB:AB:"));
    assert!(Pin::parse("sha256:abcd").is_err());
    assert!(Pin::parse(&format!("sha1:{}", "ab".repeat(32))).is_err());
    assert!(Pin::parse(&format!("sha256:{}", "zz".repeat(32))).is_err());
    assert!(tls::constant_time_eq(&p.0, &p.0));
    let mut q = p.0;
    q[31] ^= 1;
    assert!(!tls::constant_time_eq(&p.0, &q));
    assert!(!tls::constant_time_eq(&p.0, &p.0[..31]));
}

// ----------------------------------------------------------------- handshake
#[tokio::test]
async fn handshake_succeeds_and_reports_the_negotiated_session() {
    let s = fake::start().await;
    let c = client(&s);
    let info = c.connect().await.unwrap();
    assert_eq!(info.version, 1);
    assert_eq!(info.server, "fake-snp/1.0");
    assert_eq!(info.api_version, "1.9");
    assert_eq!(info.pin, s.pin());
    assert!(info.has("events") && info.has("idempotency") && info.has("files"));
    assert_eq!(info.limits.max_frame, 65_536);
    assert_eq!(info.limits.initial_window, 65_536);
    assert_eq!(info.limits.max_streams, 16);
    let r = c.call("echo", args(vec![("x", Value::Int(7))]), CallOpts::new(Retry::Safe)).await.unwrap();
    assert_eq!(r.get("args").unwrap().get("x"), Some(&Value::Int(7)));
    assert_eq!(c.connect_count(), 1);
    assert_eq!(c.status().state, "connected");
}

#[tokio::test]
async fn features_are_the_intersection_of_both_sides() {
    let s = fake::start().await;
    s.knobs.features.lock().unwrap().retain(|f| f != "idempotency" && f != "websearch");
    let info = client(&s).connect().await.unwrap();
    assert!(!info.has("idempotency") && !info.has("websearch"));
    assert!(info.has("events"));
}

#[tokio::test]
async fn pin_mismatch_reports_expected_and_presented_and_is_never_retried() {
    let s = fake::start().await;
    let other = Pin::parse(&format!("sha256:{}", "00".repeat(32))).unwrap();
    let mut cf = cfg(&s);
    cf.pin = Some(other);
    let c = SnpClient::new(cf);
    let e = c.connect().await.unwrap_err();
    match &e {
        SnpError::PinMismatch { expected, presented } => {
            assert_eq!(*expected, other);
            assert_eq!(presented.unwrap().to_string(), s.pin());
        }
        other => panic!("{other:?}"),
    }
    let msg = e.to_string();
    assert!(msg.contains(&other.to_string()) && msg.contains(&s.pin()), "{msg}");
    assert!(e.is_fatal() && e.is_security());
    assert_eq!(e.kind(), "pin_mismatch");
    // Sticky: no new connection attempt, same error.
    assert_eq!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.unwrap_err(), e);
    assert_eq!(c.open("echo", none()).await.err().unwrap(), e);
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(s.knobs.accepts.load(SeqCst), 1);
    // The handshake was refused by the TLS layer: the server never saw HELLO or AUTH.
    assert_eq!(s.knobs.app_frames.load(SeqCst), 0);
    assert_eq!(c.status().state, "failed");
    assert!(c.status().fatal);
}

#[tokio::test]
async fn wrong_token_is_unauthenticated_and_not_retried() {
    let s = fake::start().await;
    let mut cf = cfg(&s);
    cf.token_key = super::client::token_key("sk-wrong");
    let c = SnpClient::new(cf);
    let e = c.connect().await.unwrap_err();
    assert!(matches!(e, SnpError::Unauthenticated(_)), "{e:?}");
    assert!(e.is_fatal());
    assert!(e.to_string().contains("rejected the API token"));
    assert_eq!(e.http_status(), 401);
    for _ in 0..3 {
        assert!(matches!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await, Err(SnpError::Unauthenticated(_))));
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(s.knobs.accepts.load(SeqCst), 1, "no automatic re-authentication");
    // An explicit reset (user action) may try again - and fails the same way.
    c.reset().await;
    assert!(c.connect().await.is_err());
    assert_eq!(s.knobs.accepts.load(SeqCst), 2);
}

#[tokio::test]
async fn bad_server_proof_closes_the_connection_and_is_fatal() {
    let s = fake::start().await;
    s.knobs.bad_server_proof.store(true, SeqCst);
    let c = client(&s);
    let e = c.connect().await.unwrap_err();
    assert_eq!(e, SnpError::ServerProof);
    assert!(e.is_fatal() && e.is_security());
    assert!(e.to_string().contains("impostor"));
    assert!(matches!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await, Err(SnpError::ServerProof)));
    assert_eq!(s.knobs.accepts.load(SeqCst), 1);
}

#[tokio::test]
async fn version_negotiation_failure_is_fatal() {
    let s = fake::start().await;
    s.knobs.unsupported_version.store(true, SeqCst);
    let c = client(&s);
    let e = c.connect().await.unwrap_err();
    assert_eq!(e, SnpError::UnsupportedVersion { supported: vec![2, 3] });
    assert!(e.to_string().contains("[2, 3]"));
    assert!(e.is_fatal());
    assert!(c.connect().await.is_err());
    assert_eq!(s.knobs.accepts.load(SeqCst), 1);
}

#[tokio::test]
async fn tls12_only_servers_are_refused() {
    let id = fake::identity_with(&rcgen::PKCS_ECDSA_P256_SHA256, true, true);
    let s = fake::start_with(id, Knobs::new(), "127.0.0.1:0".parse().unwrap()).await;
    let e = client(&s).connect().await.unwrap_err();
    assert_eq!(e.kind(), "tls", "{e:?}");
    assert_eq!(s.knobs.app_frames.load(SeqCst), 0);
}

#[tokio::test]
async fn servers_without_alpn_snp1_are_refused() {
    let id = fake::identity_with(&rcgen::PKCS_ECDSA_P256_SHA256, false, false);
    let s = fake::start_with(id, Knobs::new(), "127.0.0.1:0".parse().unwrap()).await;
    let e = client(&s).connect().await.unwrap_err();
    assert_eq!(e, SnpError::Alpn);
    assert!(e.is_fatal());
}

#[tokio::test]
async fn ed25519_certificates_are_supported() {
    let id = fake::identity_with(&rcgen::PKCS_ED25519, false, true);
    let s = fake::start_with(id, Knobs::new(), "127.0.0.1:0".parse().unwrap()).await;
    let c = client(&s);
    c.connect().await.unwrap();
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
}

#[tokio::test]
async fn malformed_handshake_replies_are_protocol_errors() {
    type Setter = fn(&Knobs);
    let cases: [(&str, Setter, &str); 4] = [
        ("garbage", |k| k.garbage_hello_ok.store(true, SeqCst), "magic"),
        ("oversize pre-auth frame", |k| k.oversize_hello_ok.store(true, SeqCst), "exceeds the limit"),
        ("missing nonce", |k| k.missing_nonce.store(true, SeqCst), "nonce"),
        ("limits out of bounds", |k| k.out_of_range_limits.store(true, SeqCst), "max_streams"),
    ];
    for (name, set, needle) in cases {
        let s = fake::start().await;
        set(&s.knobs);
        let e = client(&s).connect().await.unwrap_err();
        match &e {
            SnpError::Protocol(msg) => assert!(msg.contains(needle), "{name}: {msg}"),
            other => panic!("{name}: {other:?}"),
        }
        assert!(!e.is_fatal(), "{name}: protocol errors back off but are not sticky");
    }
}

#[tokio::test]
async fn tofu_probe_reads_the_pin_and_never_sends_anything() {
    let s = fake::start().await;
    let r = super::probe("127.0.0.1", s.port(), None).await.unwrap();
    assert_eq!(r.pin, s.pin());
    assert_eq!(r.fingerprint, Pin::parse(&s.pin()).unwrap().fingerprint());
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(s.knobs.tls_ok.load(SeqCst), 1);
    assert_eq!(s.knobs.app_frames.load(SeqCst), 0, "a probe must not send HELLO, let alone AUTH");
    // The probe's stream is not reachable through the public API: connecting still demands an identity.
    let e = tls::connect(&tls::Endpoint { host: "127.0.0.1".into(), port: s.port(), server_name: None }, &tls::Trust::Probe, Duration::from_secs(2)).await.err().unwrap();
    assert!(matches!(e, SnpError::NoServerIdentity(_)));
}

#[tokio::test]
async fn without_a_pin_a_self_signed_certificate_is_not_trusted() {
    let s = fake::start().await;
    let mut cf = cfg(&s);
    cf.pin = None;
    cf.endpoint.server_name = Some("localhost".into());
    let e = SnpClient::new(cf).connect().await.unwrap_err();
    match &e {
        SnpError::UntrustedCertificate { presented, .. } => assert_eq!(presented.unwrap().to_string(), s.pin()),
        SnpError::NoServerIdentity(_) => {} // machine without any CA bundle
        other => panic!("{other:?}"),
    }
    assert!(e.is_fatal());
    assert_eq!(s.knobs.app_frames.load(SeqCst), 0, "nothing is sent to an unverified server");
}

#[tokio::test]
async fn unreachable_servers_give_precise_errors() {
    // Connection refused.
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    drop(l);
    let mut cf = SnpConfig::new("127.0.0.1", port, Some(Pin::parse(&format!("sha256:{}", "11".repeat(32))).unwrap()), TOKEN);
    cf.connect_timeout = Duration::from_secs(2);
    let c = SnpClient::new(cf.clone());
    let e = c.connect().await.unwrap_err();
    assert!(matches!(e, SnpError::Refused { .. }), "{e:?}");
    assert_eq!(e.kind(), "refused");
    assert!(!e.is_fatal());
    // The very next call fails fast with the back-off window instead of dialling again.
    match c.connect().await.unwrap_err() {
        SnpError::Backoff { retry_in, last } => {
            assert!(retry_in > Duration::ZERO && retry_in <= Duration::from_millis(700));
            assert!(matches!(*last, SnpError::Refused { .. }));
        }
        other => panic!("{other:?}"),
    }
    // Unresolvable name.
    cf.endpoint.host = "no-such-host.invalid".into();
    let e = SnpClient::new(cf).connect().await.unwrap_err();
    assert!(matches!(e, SnpError::Dns { .. }) || matches!(e, SnpError::Timeout { .. }), "{e:?}");
    // A server that accepts TCP but never answers TLS.
    let silent = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let sp = silent.local_addr().unwrap().port();
    tokio::spawn(async move {
        let mut keep = vec![];
        while let Ok((s, _)) = silent.accept().await {
            keep.push(s);
        }
    });
    let mut cf = SnpConfig::new("127.0.0.1", sp, Some(Pin::parse(&format!("sha256:{}", "11".repeat(32))).unwrap()), TOKEN);
    cf.connect_timeout = Duration::from_millis(300);
    let e = SnpClient::new(cf).connect().await.unwrap_err();
    assert_eq!(e, SnpError::Timeout { stage: "tls".into() });
}

// ------------------------------------------------------------------- streams
#[tokio::test]
async fn concurrent_unary_calls_use_increasing_odd_stream_ids() {
    let s = fake::start().await;
    let c = client(&s);
    let mut tasks = vec![];
    for i in 0..24 {
        let c = c.clone();
        tasks.push(tokio::spawn(async move { c.call("echo", args(vec![("i", Value::Int(i))]), CallOpts::new(Retry::Safe)).await }));
    }
    for (i, t) in tasks.into_iter().enumerate() {
        let r = t.await.unwrap().unwrap();
        assert_eq!(r.get("args").unwrap().get("i"), Some(&Value::Int(i as i64)));
    }
    assert!(!s.knobs.order_violation.load(SeqCst));
    let ids: Vec<u32> = s.knobs.opens.lock().unwrap().iter().map(|o| o.stream).collect();
    assert_eq!(ids.len(), 24);
    assert!(ids.iter().all(|i| i % 2 == 1));
    assert!(ids.windows(2).all(|w| w[0] < w[1]), "ids appear on the wire in increasing order: {ids:?}");
    assert_eq!(c.connect_count(), 1, "one persistent connection");
}

#[tokio::test]
async fn stream_mode_delivers_messages_in_order_then_end() {
    let s = fake::start().await;
    let c = client(&s);
    let mut st = c.open("count", args(vec![("n", Value::Int(50))])).await.unwrap();
    let (msgs, _, end) = drain_to_end(&mut st).await;
    assert_eq!(msgs.len(), 50);
    for (i, v) in msgs.iter().enumerate() {
        assert_eq!(v.get("i"), Some(&Value::Int(i as i64)));
    }
    assert_eq!(end, Value::Null);
    assert!(matches!(st.recv().await, Err(SnpError::StreamClosed)));
}

#[tokio::test]
async fn remote_errors_are_typed_and_keep_the_connection() {
    let s = fake::start().await;
    let c = client(&s);
    let e = c.call("x.unknown", none(), CallOpts::new(Retry::Safe)).await.unwrap_err();
    match &e {
        SnpError::Remote(r) => assert_eq!((r.code.as_str(), r.status, r.retryable), ("unknown_op", 404, false)),
        other => panic!("{other:?}"),
    }
    assert_eq!(e.http_status(), 404);
    s.knobs.script("echo", vec![Reply::Error { code: "conflict".into(), status: 409, message: "already running".into(), retryable: false, detail: Some(m(vec![("job", Value::text("j1"))])) }]);
    match c.call("echo", none(), CallOpts::new(Retry::No)).await.unwrap_err() {
        SnpError::Remote(r) => {
            assert_eq!(r.status, 409);
            assert_eq!(r.message, "already running");
            assert_eq!(r.detail.unwrap().get("job"), Some(&Value::text("j1")));
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(c.connect_count(), 1);
}

#[tokio::test]
async fn cancel_stops_the_stream_and_keeps_the_connection() {
    let s = fake::start().await;
    let c = client(&s);
    let mut st = c.open("hang", none()).await.unwrap();
    let id = st.id();
    tokio::time::sleep(Duration::from_millis(50)).await;
    st.cancel();
    eventually("server saw CANCEL", || s.knobs.cancels.lock().unwrap().contains(&id)).await;
    assert!(matches!(st.recv().await, Err(SnpError::StreamClosed)));
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    assert_eq!(c.connect_count(), 1);
}

#[tokio::test]
async fn dropping_a_stream_sends_cancel() {
    let s = fake::start().await;
    let c = client(&s);
    let st = c.open("hang", none()).await.unwrap();
    let id = st.id();
    drop(st);
    eventually("server saw CANCEL", || s.knobs.cancels.lock().unwrap().contains(&id)).await;
}

#[tokio::test]
async fn deadline_is_sent_in_open_and_reported() {
    let s = fake::start().await;
    let c = client(&s);
    let mut o = CallOpts::new(Retry::No);
    o.deadline = Some(Duration::from_millis(300));
    let t0 = Instant::now();
    let e = c.call("hang", none(), o).await.unwrap_err();
    assert!(t0.elapsed() < Duration::from_secs(3));
    match e {
        SnpError::Remote(r) => assert_eq!((r.code.as_str(), r.status), ("deadline_exceeded", 504)),
        other => panic!("{other:?}"),
    }
    assert_eq!(s.knobs.opens_of("hang")[0].deadline_ms, Some(300));
}

#[tokio::test]
async fn stream_slots_apply_backpressure_to_new_opens() {
    let s = fake::start().await;
    s.knobs.limits(|l| l.max_streams = 2);
    let c = client(&s);
    let mut a = c.open("hang", none()).await.unwrap();
    let _b = c.open("hang", none()).await.unwrap();
    let c2 = c.clone();
    let third = tokio::spawn(async move { c2.open("hang", none()).await.map(|_| ()) });
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert!(!third.is_finished(), "the third open waits for a free slot");
    a.cancel();
    tokio::time::timeout(Duration::from_secs(3), third).await.expect("slot released").unwrap().unwrap();
}

#[tokio::test]
async fn oversized_requests_are_rejected_locally() {
    let s = fake::start().await;
    let c = client(&s);
    let big = Value::Bytes(vec![0u8; 100_000]);
    let e = c.call("echo", args(vec![("blob", big)]), CallOpts::new(Retry::Safe)).await.unwrap_err();
    assert!(matches!(e, SnpError::Limit(_)), "{e:?}");
    assert!(e.to_string().contains("65536"));
    assert!(s.knobs.opens_of("echo").is_empty());
}

#[tokio::test]
async fn malformed_stream_payload_terminates_only_that_stream() {
    let s = fake::start().await;
    let c = client(&s);
    let mut st = c.open("malformed_msg", none()).await.unwrap();
    let id = st.id();
    match st.recv().await {
        Err(SnpError::Protocol(m)) => assert!(m.contains("malformed"), "{m}"),
        other => panic!("{other:?}"),
    }
    eventually("server saw CANCEL", || s.knobs.cancels.lock().unwrap().contains(&id)).await;
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    assert_eq!(c.connect_count(), 1, "connection survived");
}

// -------------------------------------------------------------- flow control
#[tokio::test]
async fn a_slow_consumer_stops_the_server_from_sending() {
    let s = fake::start().await;
    s.knobs.limits(|l| {
        l.initial_window = 32_768;
        l.conn_window = 65_536;
    });
    let c = client(&s);
    let size = 1 << 20;
    let mut st = c.open("blob", args(vec![("size", Value::Int(size)), ("chunk", Value::Int(8192))])).await.unwrap();
    match st.recv().await.unwrap() {
        Event::Msg(v) => assert_eq!(v.get("meta").unwrap().get("size"), Some(&Value::Int(size))),
        other => panic!("{other:?}"),
    }
    // The consumer now stops reading. Credit is only returned after consumption, so the
    // server can have at most one window outstanding.
    tokio::time::sleep(Duration::from_millis(400)).await;
    let sent = s.knobs.blob_sent.load(SeqCst);
    assert!(sent <= 32_768, "server sent {sent} bytes without credit");
    assert!(sent >= 16_384, "server should use the credit it has: {sent}");
    assert!(!s.knobs.blob_done.load(SeqCst));
    // No head-of-line blocking: other streams on the same connection keep working.
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    // Consuming releases the rest, intact.
    let (_, data, _) = drain_to_end(&mut st).await;
    assert_eq!(data.len(), size as usize);
    assert!(data.iter().enumerate().all(|(i, b)| *b == (i % 251) as u8));
    assert!(s.knobs.blob_done.load(SeqCst));
    assert_eq!(c.connect_count(), 1);
}

#[tokio::test]
async fn upload_obeys_the_servers_windows() {
    let s = fake::start().await;
    s.knobs.limits(|l| {
        l.initial_window = 16_384;
        l.conn_window = 32_768;
    });
    s.knobs.sink_delay_ms.store(1, SeqCst);
    let c = client(&s);
    let st = c.open("file.upload", args(vec![("name", Value::text("a.bin"))])).await.unwrap();
    let total = 300 * 1024;
    let payload: Vec<u8> = (0..total).map(|i| (i % 253) as u8).collect();
    tokio::time::timeout(Duration::from_secs(20), st.send_data(Bytes::from(payload))).await.expect("upload stalled").unwrap();
    st.end().unwrap();
    let mut st = st;
    let (_, _, end) = drain_to_end(&mut st).await;
    assert_eq!(end.get("size"), Some(&Value::Int(total as i64)));
    assert_eq!(s.knobs.upload_bytes.load(SeqCst), total as u64);
    assert!(!s.knobs.client_overran_credit.load(SeqCst), "the client exceeded the credit it was given");
}

#[tokio::test]
async fn upload_interrupted_by_cancel_is_discarded() {
    let s = fake::start().await;
    let c = client(&s);
    let st = c.open("file.upload", none()).await.unwrap();
    st.send_data(Bytes::from(vec![1u8; 20_000])).await.unwrap();
    drop(st);
    eventually("server saw the cancel", || s.knobs.upload_cancelled.load(SeqCst)).await;
    assert!(!s.knobs.upload_done.load(SeqCst));
    assert!(s.knobs.upload_bytes.load(SeqCst) <= 20_000);
}

#[tokio::test]
async fn upload_interrupted_by_connection_loss_fails_the_stream() {
    let s = fake::start().await;
    let c = client(&s);
    let mut st = c.open("file.upload", none()).await.unwrap();
    st.send_data(Bytes::from(vec![1u8; 10_000])).await.unwrap();
    eventually("data arrived", || s.knobs.upload_bytes.load(SeqCst) == 10_000).await;
    s.knobs.kill_all();
    match tokio::time::timeout(Duration::from_secs(3), st.recv()).await.unwrap() {
        Err(SnpError::ConnectionLost(_)) => {}
        other => panic!("{other:?}"),
    }
    assert!(st.send_data(Bytes::from(vec![0u8; 10])).await.is_err());
    eventually("server noticed", || s.knobs.upload_eof.load(SeqCst)).await;
    assert!(!s.knobs.upload_done.load(SeqCst));
}

#[tokio::test]
async fn upload_too_large_is_reported_as_soon_as_the_server_says_so() {
    let s = fake::start().await;
    let c = client(&s);
    let mut st = c.open("file.upload", args(vec![("limit", Value::Int(100_000))])).await.unwrap();
    let mut failed = false;
    for _ in 0..64 {
        if st.send_data(Bytes::from(vec![0u8; 32_768])).await.is_err() {
            failed = true;
            break;
        }
    }
    assert!(failed, "sending must stop once the stream is closed by the server");
    match st.recv().await {
        Err(SnpError::Remote(r)) => assert_eq!((r.code.as_str(), r.status), ("too_large", 413)),
        other => panic!("{other:?}"),
    }
}

async fn raw_case(bytes: Vec<u8>, expect_goaway: &str) -> (Server, SnpClient, SnpError) {
    raw_case_on(fake::start().await, bytes, expect_goaway).await
}

async fn raw_case_on(s: Server, bytes: Vec<u8>, expect_goaway: &str) -> (Server, SnpClient, SnpError) {
    let c = client(&s);
    let mut st = c.open("raw", args(vec![("bytes", Value::Bytes(bytes))])).await.unwrap();
    let e = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Err(e) = st.recv().await {
                return e;
            }
        }
    })
    .await
    .expect("client must react");
    eventually("server received GOAWAY from the client", || s.knobs.goaways_rx.lock().unwrap().iter().any(|c| c == expect_goaway)).await;
    (s, c, e)
}

#[tokio::test]
async fn window_overflow_is_a_protocol_error() {
    let inc = cbor::encode(&m(vec![("inc", Value::Int((1 << 31) - 1))])).unwrap();
    let (_s, c, e) = raw_case(fake::frame(0x16, 0, &inc), "protocol_error").await;
    assert!(matches!(&e, SnpError::Protocol(m) if m.contains("window")), "{e:?}");
    // Protocol errors are not sticky: the client backs off, then reconnects.
    assert!(!e.is_fatal());
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    assert_eq!(c.connect_count(), 2);
}

#[tokio::test]
async fn data_beyond_granted_credit_is_a_protocol_error() {
    // Stream 1 is the stream carrying the `raw` request itself: its window is 16 384 octets.
    let s = fake::start().await;
    s.knobs.limits(|l| l.initial_window = 16_384);
    let mut bytes = fake::frame(0x12, 1, &vec![0u8; 10_000]);
    bytes.extend(fake::frame(0x12, 1, &vec![0u8; 10_000]));
    let (_s, _c, e) = raw_case_on(s, bytes, "protocol_error").await;
    assert!(matches!(&e, SnpError::Protocol(m) if m.contains("window")), "{e:?}");
}

#[tokio::test]
async fn frames_larger_than_the_negotiated_maximum_are_refused_from_the_header() {
    // Header only: 65 537 > max_frame, and the payload is never sent.
    let mut h = fake::frame(0x12, 1, &[]);
    h[8..12].copy_from_slice(&65_537u32.to_be_bytes());
    let (_s, _c, e) = raw_case(h, "frame_too_large").await;
    assert!(matches!(&e, SnpError::Protocol(m) if m.contains("exceeds")), "{e:?}");
}

#[tokio::test]
async fn invalid_frames_from_the_server_close_the_connection() {
    let mut bad_magic = fake::frame(0x11, 1, &[0xa0]);
    bad_magic[1] = 0x4f;
    let mut bad_flags = fake::frame(0x11, 1, &[0xa0]);
    bad_flags[3] = 1;
    let unknown_type = fake::frame(0x99, 1, &[]);
    let even_stream = fake::frame(0x11, 2, &[0xa0]);
    let client_only_type = fake::frame(0x10, 1, &[0xa0]); // OPEN is client -> server only
    for (name, bytes) in [("magic", bad_magic), ("flags", bad_flags), ("type", unknown_type), ("even stream id", even_stream), ("direction", client_only_type)] {
        let (_s, _c, e) = raw_case(bytes, "protocol_error").await;
        assert!(matches!(e, SnpError::Protocol(_)), "{name}: {e:?}");
    }
}

#[tokio::test]
async fn late_frames_for_closed_streams_are_discarded_without_leaking_credit() {
    let s = fake::start().await;
    s.knobs.limits(|l| {
        l.initial_window = 16_384;
        l.conn_window = 16_384;
    });
    let c = client(&s);
    // Open a download, take the metadata, abandon it while the server still has data in flight, repeat:
    // if the connection credit of discarded DATA frames were not returned, the 16 KiB window would run dry.
    for _ in 0..8 {
        let mut st = c.open("blob", args(vec![("size", Value::Int(200_000)), ("chunk", Value::Int(4096))])).await.unwrap();
        let _ = st.recv().await.unwrap();
        tokio::time::sleep(Duration::from_millis(30)).await;
        drop(st);
    }
    let mut st = c.open("blob", args(vec![("size", Value::Int(100_000)), ("chunk", Value::Int(4096))])).await.unwrap();
    let (_, data, _) = tokio::time::timeout(Duration::from_secs(10), async { drain_to_end(&mut st).await }).await.expect("connection window leaked");
    assert_eq!(data.len(), 100_000);
    assert_eq!(c.connect_count(), 1);
}

// ----------------------------------------------------------------- liveness
#[tokio::test]
async fn idle_connections_send_pings_and_stay_alive() {
    let s = fake::start().await;
    s.knobs.limits(|l| {
        l.ping_interval_ms = 1000;
        l.idle_timeout_ms = 5000;
    });
    let c = client(&s);
    c.connect().await.unwrap();
    tokio::time::sleep(Duration::from_millis(2400)).await;
    assert!(s.knobs.pings_rx.load(SeqCst) >= 2, "pings: {}", s.knobs.pings_rx.load(SeqCst));
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    assert_eq!(c.connect_count(), 1);
}

#[tokio::test]
async fn a_silent_server_is_declared_dead_after_the_idle_timeout() {
    let s = fake::start().await;
    s.knobs.limits(|l| {
        l.ping_interval_ms = 1000;
        l.idle_timeout_ms = 2000;
    });
    let c = client(&s);
    let mut st = c.open("hang", none()).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    s.knobs.mute.store(true, SeqCst);
    let t0 = Instant::now();
    let e = tokio::time::timeout(Duration::from_secs(6), st.recv()).await.expect("idle timeout must fire").unwrap_err();
    assert!(matches!(e, SnpError::ConnectionLost(ref m) if m.contains("no data")), "{e:?}");
    assert!(t0.elapsed() >= Duration::from_millis(1500) && t0.elapsed() < Duration::from_secs(4), "{:?}", t0.elapsed());
    assert!(s.knobs.pings_rx.load(SeqCst) >= 1);
    eventually("client told the server why", || s.knobs.goaways_rx.lock().unwrap().iter().any(|c| c == "idle_timeout")).await;
}

#[tokio::test]
async fn the_client_answers_server_pings_with_the_same_octets() {
    let s = fake::start().await;
    let c = client(&s);
    c.call("ping_me", none(), CallOpts::new(Retry::Safe)).await.unwrap();
    eventually("PONG", || s.knobs.pongs_rx.lock().unwrap().iter().any(|p| p == &[9, 8, 7, 6, 5, 4, 3, 2])).await;
}

// ----------------------------------------------------------------- reconnect
#[test]
fn backoff_delays_grow_cap_and_jitter() {
    let p = BackoffPolicy::default();
    let d = |n, u| p.delay(n, u).as_millis();
    assert_eq!((d(1, 0.0), d(2, 0.0), d(3, 0.0), d(4, 0.0)), (500, 1000, 2000, 4000));
    assert_eq!(d(7, 0.0), 30_000, "capped at 30 s");
    assert_eq!(d(60, 0.0), 30_000);
    assert_eq!((d(1, 1.0), d(1, -1.0)), (625, 375), "±25 % jitter");
    assert_eq!(d(7, 1.0), 37_500);
    let seq: Vec<_> = (1..12).map(|n| d(n, 0.0)).collect();
    assert!(seq.windows(2).all(|w| w[0] <= w[1]));
    assert_eq!(p.delay(1, 9.0), p.delay(1, 1.0), "jitter input is clamped");
}

#[tokio::test]
async fn reconnects_with_backoff_after_the_server_restarts() {
    let id = fake::identity();
    let knobs = Knobs::new();
    let s = fake::start_with(id.clone(), knobs.clone(), "127.0.0.1:0".parse().unwrap()).await;
    let addr = s.addr;
    let c = client(&s);
    assert!(c.call("echo", none(), CallOpts::new(Retry::Safe)).await.is_ok());
    s.stop();
    tokio::time::sleep(Duration::from_millis(100)).await;
    // Connection loss is noticed; the first redial is refused, then calls fail fast inside the back-off window.
    let mut saw_backoff = false;
    let mut errors = 0;
    for _ in 0..12 {
        tokio::time::sleep(Duration::from_millis(80)).await;
        match c.call("echo", none(), CallOpts::new(Retry::No)).await {
            Err(SnpError::Backoff { retry_in, .. }) => {
                assert!(retry_in <= Duration::from_millis(600));
                saw_backoff = true;
            }
            Err(_) => errors += 1,
            Ok(_) => panic!("server is down"),
        }
    }
    assert!(errors >= 1 && saw_backoff, "errors {errors}, backoff {saw_backoff}");
    let dials_while_down = knobs.accepts.load(SeqCst);
    assert_eq!(dials_while_down, 1, "refused redials never reached the (stopped) server");
    // Server returns on the same address with the same identity: the client recovers by itself.
    let s2 = fake::start_with(id, Knobs::new(), addr).await;
    let t0 = Instant::now();
    loop {
        match c.call("echo", none(), CallOpts::new(Retry::Safe)).await {
            Ok(_) => break,
            Err(_) if t0.elapsed() < Duration::from_secs(5) => tokio::time::sleep(Duration::from_millis(50)).await,
            Err(e) => panic!("did not recover: {e}"),
        }
    }
    assert_eq!(s2.knobs.accepts.load(SeqCst), 1);
    assert_eq!(c.connect_count(), 2);
}

#[tokio::test]
async fn idempotent_retry_reuses_the_key_across_the_reconnect() {
    let s = fake::start().await;
    let c = client(&s);
    c.connect().await.unwrap();
    s.knobs.drop_next.store(1, SeqCst);
    let r = c.call("mutate", args(vec![("v", Value::Int(1))]), CallOpts::new(Retry::Idem)).await.unwrap();
    let opens = s.knobs.opens_of("mutate");
    assert_eq!(opens.len(), 2, "one attempt per connection");
    assert_ne!(opens[0].conn, opens[1].conn);
    let (k1, k2) = (opens[0].idem.clone().unwrap(), opens[1].idem.clone().unwrap());
    assert_eq!(k1, k2, "same logical operation, same key");
    assert!(k1.len() >= 32 && k1.chars().all(|c| c.is_ascii_hexdigit()), "128-bit key: {k1}");
    assert_eq!(r.get("idem").and_then(Value::as_str), Some(k1.as_str()));
    // A different logical operation gets a fresh key.
    c.call("mutate", none(), CallOpts::new(Retry::Idem)).await.unwrap();
    let last = s.knobs.opens_of("mutate").pop().unwrap();
    assert_ne!(last.idem.unwrap(), k1);
}

#[tokio::test]
async fn reads_are_retried_without_keys_and_mutations_without_keys_are_not_retried() {
    let s = fake::start().await;
    let c = client(&s);
    c.connect().await.unwrap();
    s.knobs.drop_next.store(1, SeqCst);
    c.call("get_thing", none(), CallOpts::new(Retry::Safe)).await.unwrap();
    let opens = s.knobs.opens_of("get_thing");
    assert_eq!(opens.len(), 2);
    assert!(opens.iter().all(|o| o.idem.is_none()), "GET-equivalents carry no key");

    tokio::time::sleep(Duration::from_millis(300)).await;
    s.knobs.drop_next.store(1, SeqCst);
    let e = c.call("other", none(), CallOpts::new(Retry::No)).await.unwrap_err();
    assert!(matches!(e, SnpError::ConnectionLost(_)), "{e:?}");
    assert_eq!(s.knobs.opens_of("other").len(), 1, "Retry::No never re-sends");

    // Without the idempotency feature a mutation must not be re-sent either.
    let s = fake::start().await;
    s.knobs.features.lock().unwrap().retain(|f| f != "idempotency");
    let c = client(&s);
    c.connect().await.unwrap();
    s.knobs.drop_next.store(1, SeqCst);
    assert!(c.call("mutate", none(), CallOpts::new(Retry::Idem)).await.is_err());
    assert_eq!(s.knobs.opens_of("mutate").len(), 1);
    assert!(s.knobs.opens_of("mutate")[0].idem.is_none());
}

#[tokio::test]
async fn retryable_server_errors_are_retried_with_the_same_key() {
    let s = fake::start().await;
    s.knobs.script("mutate", vec![Reply::Error { code: "resource_exhausted".into(), status: 503, message: "busy".into(), retryable: true, detail: None }]);
    let c = client(&s);
    let e = c.call("mutate", none(), CallOpts::new(Retry::Idem)).await.unwrap_err();
    assert!(matches!(e, SnpError::Remote(ref r) if r.status == 503));
    let opens = s.knobs.opens_of("mutate");
    assert_eq!(opens.len(), 3);
    assert!(opens.iter().all(|o| o.idem == opens[0].idem && o.idem.is_some()));
    // Non-retryable server errors are returned at once.
    s.knobs.script("get_thing", vec![Reply::Error { code: "not_found".into(), status: 404, message: "nope".into(), retryable: false, detail: None }]);
    assert!(c.call("get_thing", none(), CallOpts::new(Retry::Safe)).await.is_err());
    assert_eq!(s.knobs.opens_of("get_thing").len(), 1);
}

#[tokio::test]
async fn goaway_shutdown_makes_unprocessed_streams_retryable_on_a_new_connection() {
    let s = fake::start().await;
    s.knobs.goaway_next.store(1, SeqCst);
    let c = client(&s);
    let r = c.call("later", none(), CallOpts::new(Retry::Safe)).await.unwrap();
    assert_eq!(r.get("op").and_then(Value::as_str), Some("later"));
    assert_eq!(s.knobs.opens_of("later").len(), 2);
    assert_eq!(s.knobs.accepts.load(SeqCst), 2);
    // Even a non-idempotent call is re-sent: the server said it never processed the stream.
    s.knobs.goaway_next.store(1, SeqCst);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(c.call("later", none(), CallOpts::new(Retry::No)).await.is_ok());
}

// -------------------------------------------------------------------- events
#[tokio::test]
async fn event_subscription_delivers_controls_and_resubscribes_after_reconnect() {
    let s = fake::start().await;
    let c = client(&s);
    let mut sub = c.subscribe_events(vec!["job.".into()], true).await.unwrap();
    match sub.next().await {
        SubEvent::Event(v) => assert_eq!(v.get("type").and_then(Value::as_str), Some("hello")),
        other => panic!("{other:?}"),
    }
    assert_eq!(s.knobs.opens_of("event.subscribe")[0].args.get("topics"), Some(&Value::Array(vec![Value::text("job.")])));
    eventually("subscriber registered", || s.knobs.events.receiver_count() == 1).await;
    s.knobs.events.send(m(vec![("type", Value::text("job.updated")), ("ts", Value::Float(1.5)), ("data", m(vec![("id", Value::text("j1"))]))])).unwrap();
    match sub.next().await {
        SubEvent::Event(v) => assert_eq!(v.get("data").unwrap().get("id"), Some(&Value::text("j1"))),
        other => panic!("{other:?}"),
    }
    sub.update_topics(vec!["download.".into()], vec!["job.".into()]).await.unwrap();
    eventually("control message", || !s.knobs.ctrl_msgs.lock().unwrap().is_empty()).await;
    let ctrl = s.knobs.ctrl_msgs.lock().unwrap()[0].clone();
    assert_eq!(ctrl.get("subscribe"), Some(&Value::Array(vec![Value::text("download.")])));
    assert_eq!(ctrl.get("unsubscribe"), Some(&Value::Array(vec![Value::text("job.")])));

    s.knobs.kill_all();
    assert!(matches!(sub.next().await, SubEvent::Disconnected(_)));
    assert!(matches!(tokio::time::timeout(Duration::from_secs(5), sub.next()).await.unwrap(), SubEvent::Resubscribed));
    match sub.next().await {
        SubEvent::Event(v) => assert_eq!(v.get("type").and_then(Value::as_str), Some("hello")),
        other => panic!("{other:?}"),
    }
    let opens = s.knobs.opens_of("event.subscribe");
    assert_eq!(opens.len(), 2);
    assert_eq!(opens[1].args.get("topics"), Some(&Value::Array(vec![Value::text("download.")])), "re-subscribes with the updated topics");
    assert_ne!(opens[0].conn, opens[1].conn);
}

#[tokio::test]
async fn subscription_without_auto_resubscribe_reports_the_loss_once_and_stays_down() {
    let s = fake::start().await;
    let c = client(&s);
    let mut sub = c.subscribe_events(vec![], false).await.unwrap();
    assert!(matches!(sub.next().await, SubEvent::Event(_)));
    s.knobs.kill_all();
    assert!(matches!(sub.next().await, SubEvent::Disconnected(SnpError::ConnectionLost(_))));
    assert!(matches!(sub.next().await, SubEvent::Disconnected(SnpError::StreamClosed)));
    assert_eq!(s.knobs.opens_of("event.subscribe").len(), 1);
}

#[tokio::test]
async fn subscription_requires_the_events_feature() {
    let s = fake::start().await;
    s.knobs.features.lock().unwrap().retain(|f| f != "events");
    let e = client(&s).subscribe_events(vec![], true).await.err().unwrap();
    assert!(matches!(e, SnpError::Protocol(ref m) if m.contains("events")));
}

// ------------------------------------------------------------ test connection
#[tokio::test]
async fn test_connection_reports_the_negotiated_session_without_secrets() {
    let s = fake::start().await;
    let r = crate::check_snp(cfg(&s)).await;
    assert!(r.ok, "{}", r.message);
    assert_eq!((r.stage.as_str(), r.transport.as_str()), ("ok", "snp"));
    let info = r.snp.as_ref().unwrap();
    assert_eq!((info.version, info.server.as_str(), info.api_version.as_str()), (1, "fake-snp/1.0", "1.9"));
    assert!(info.features.iter().any(|f| f == "events"));
    assert_eq!(info.limits.max_frame, 65_536);
    assert_eq!(info.pin, s.pin());
    assert_eq!(info.fingerprint, Pin::parse(&s.pin()).unwrap().fingerprint());
    assert!(r.latency_ms.is_some() && r.health.is_some() && r.system.is_some());
    let json = serde_json::to_string(&r).unwrap();
    assert!(!json.contains(TOKEN), "the API token must never be reported");
    assert!(!json.contains(&hex::encode(super::client::token_key(TOKEN))), "nor its hash");
    assert_eq!(s.knobs.opens_of("system.health").len(), 1);
}

#[tokio::test]
async fn test_connection_failures_name_the_stage_and_carry_both_pins() {
    let s = fake::start().await;
    let wrong = Pin::parse(&format!("sha256:{}", "00".repeat(32))).unwrap();
    let mut c = cfg(&s);
    c.pin = Some(wrong);
    let r = crate::check_snp(c).await;
    assert!(!r.ok);
    assert_eq!((r.stage.as_str(), r.error_kind.as_deref()), ("identity", Some("pin_mismatch")));
    assert_eq!(r.expected_pin.as_deref(), Some(wrong.to_string().as_str()));
    assert_eq!(r.presented_pin.as_deref(), Some(s.pin().as_str()));
    assert!(r.message.contains(&s.pin()) && r.message.contains(&wrong.to_string()));

    let mut c = cfg(&s);
    c.token_key = super::client::token_key("nope");
    let r = crate::check_snp(c).await;
    assert_eq!((r.stage.as_str(), r.error_kind.as_deref()), ("auth", Some("auth")));

    s.knobs.unsupported_version.store(true, SeqCst);
    let r = crate::check_snp(cfg(&s)).await;
    assert_eq!((r.stage.as_str(), r.error_kind.as_deref()), ("version", Some("version")));

    s.knobs.unsupported_version.store(false, SeqCst);
    s.knobs.bad_server_proof.store(true, SeqCst);
    let r = crate::check_snp(cfg(&s)).await;
    assert_eq!((r.stage.as_str(), r.error_kind.as_deref()), ("server_proof", Some("server_proof")));

    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    drop(l);
    let r = crate::check_snp(SnpConfig::new("127.0.0.1", port, Some(wrong), TOKEN)).await;
    assert_eq!((r.stage.as_str(), r.error_kind.as_deref()), ("connect", Some("refused")));
}

// ===========================================================================
// Loopback adapter (HTTP/JSON -> SNP) through the real proxy
// ===========================================================================
mod adapter_tests {
    use super::*;
    use crate::proxy::{self, ProxyShared, Upstream};
    use crate::snp::adapter::{self, OpMode, SnpBackend};
    use futures_util::{SinkExt, StreamExt};

    struct Rig {
        server: Server,
        shared: Arc<ProxyShared>,
        base: String,
        http: reqwest::Client,
        dir: tempfile::TempDir,
    }

    impl Rig {
        async fn new() -> Rig {
            Self::with(fake::start().await).await
        }
        async fn with(server: Server) -> Rig {
            let dir = tempfile::tempdir().unwrap();
            let shared = ProxyShared::new(dir.path().to_path_buf());
            let port = proxy::serve(shared.clone()).await.unwrap();
            *shared.snp.write().await = Some(Arc::new(SnpBackend { client: client(&server) }));
            Rig { server, shared, base: format!("http://127.0.0.1:{port}"), http: reqwest::Client::new(), dir }
        }
        fn url(&self, path: &str) -> String {
            let sep = if path.contains('?') { '&' } else { '?' };
            format!("{}{}{}s={}", self.base, path, sep, self.shared.session)
        }
        fn req(&self, method: &str, path: &str) -> reqwest::RequestBuilder {
            self.http.request(method.parse().unwrap(), self.url(path))
        }
    }

    #[test]
    fn route_table_cases() {
        let t = adapter::table();
        let name = |m: &str, p: &str| t.route(m, p).map(|r| r.op.op.clone());
        assert_eq!(name("DELETE", "/v1/jobs/finished").as_deref(), Some("job.clear_finished"));
        assert_eq!(name("POST", "/v1/jobs/j_1/cancel").as_deref(), Some("job.cancel"));
        assert_eq!(name("GET", "/v1/jobs/j_1").as_deref(), Some("job.get"));
        assert_eq!(name("GET", "/v1/jobs/").as_deref(), Some("job.list"), "trailing slash");
        assert_eq!(name("PUT", "/v1/jobs"), None, "wrong method");
        assert_eq!(name("GET", "/v1/nope"), None);
        assert_eq!(name("GET", "/v2/jobs"), None);
        let r = t.route("DELETE", "/v1/models/org/name/file.gguf").unwrap();
        assert_eq!(r.op.op, "model.delete");
        assert_eq!(r.path_params, vec![("model_id".to_string(), "org/name/file.gguf".to_string())]);
        let r = t.route("GET", "/v1/chat/conversations/c%201").unwrap();
        assert_eq!(r.path_params, vec![("cid".to_string(), "c 1".to_string())]);
        let r = t.route("POST", "/v1/downloads/d_9/pause").unwrap();
        assert_eq!((r.op.op.as_str(), r.path_params.len()), ("download.action", 2));
        assert_eq!(name("GET", "/v1/experiments/paintbrush/status").as_deref(), Some("paintbrush.status"));
        assert_eq!(name("POST", "/v1/experiments/paintbrush/renders/r1/save").as_deref(), Some("paintbrush.render.save"));
        assert_eq!(name("GET", "/v1/files/f_1").as_deref(), Some("file.get"));
        assert_eq!(name("POST", "/v1/files/f_1/sign").as_deref(), Some("file.sign"));
        assert_eq!(name("GET", "/v1/events").as_deref(), Some("event.subscribe"));
    }

    #[test]
    fn every_catalogue_operation_is_well_formed_and_reachable() {
        let t = adapter::table();
        assert!(t.ops.len() > 50, "the catalogue is unexpectedly small");
        let mut names = std::collections::HashSet::new();
        let mut routes = std::collections::HashSet::new();
        for op in &t.ops {
            assert!(names.insert(op.op.clone()), "duplicate op {}", op.op);
            assert!(routes.insert((op.method.clone(), op.route.clone())), "duplicate route {} {}", op.method, op.route);
            assert!(op.route.starts_with("/v1/"));
            // Build a concrete URL from the template and make sure it routes back to the same op.
            let path: String = op
                .route
                .split('/')
                .map(|seg| match seg.strip_prefix('{').and_then(|s| s.strip_suffix('}')) {
                    Some(inner) if inner.ends_with(":path") => "a/b".to_string(),
                    Some(_) => "x1".to_string(),
                    None => seg.to_string(),
                })
                .collect::<Vec<_>>()
                .join("/");
            let r = t.route(&op.method, &path).unwrap_or_else(|| panic!("{} {} is unreachable", op.method, path));
            assert_eq!(r.op.op, op.op, "{} {} routed to the wrong op", op.method, path);
            // Every path parameter is declared in `params`; the body goes to `body` / `patch`.
            for p in op.path_param_names() {
                assert!(op.params.iter().any(|x| x == p), "{}: path parameter {p} not in params", op.op);
            }
        }
        let modes: Vec<(&str, OpMode)> = t.ops.iter().filter(|o| o.mode != OpMode::Unary).map(|o| (o.op.as_str(), o.mode)).collect();
        assert_eq!(
            modes,
            vec![
                ("chat.generate", OpMode::Stream),
                ("agent.completion", OpMode::Stream),
                ("file.get", OpMode::Download),
                ("file.upload", OpMode::Upload),
                ("event.subscribe", OpMode::Subscribe)
            ]
        );
        assert_eq!(t.by_name("settings.patch").unwrap().body_key, Some("patch"));
        assert_eq!(t.by_name("job.retry").unwrap().body_key, Some("body"));
        assert_eq!(t.by_name("job.get").unwrap().body_key, None);
    }

    #[test]
    fn templates_reject_malformed_catalogues() {
        let bad = |ops: &str| adapter::OpTable::parse(&format!(r#"{{"protocol":"SNP/1","ops":[{ops}]}}"#)).err();
        assert!(bad(r#"{"op":"a","method":"GET","route":"/v1/{x","mode":"unary","params":[]}"#).is_some());
        assert!(bad(r#"{"op":"a","method":"GET","route":"v1/x","mode":"unary","params":[]}"#).is_some());
        assert!(bad(r#"{"op":"a","method":"GET","route":"/v1/{x:int}","mode":"unary","params":[]}"#).is_some());
        assert!(bad(r#"{"op":"a","method":"GET","route":"/v1/x","mode":"weird","params":[]}"#).is_some());
        assert!(bad(r#"{"op":"a","method":"GET","route":"/v1/x","mode":"unary","params":[]}"#).is_none());
        assert!(adapter::OpTable::parse(r#"{"protocol":"SNP/2","ops":[]}"#).is_err());
    }

    #[test]
    fn query_values_are_typed_and_path_params_win() {
        let t = adapter::table();
        let routed = t.route("GET", "/v1/generations").unwrap();
        let a = adapter::build_args(&routed, Some("limit=5&favorite=true&since=1.5&q=a+b&unknown=1&s=secret&trash=maybe"), None);
        assert_eq!(a.get("limit"), Some(&Value::Int(5)));
        assert_eq!(a.get("favorite"), Some(&Value::Bool(true)));
        assert_eq!(a.get("since"), Some(&Value::Float(1.5)));
        assert_eq!(a.get("q"), Some(&Value::text("a b")));
        assert_eq!(a.get("trash"), Some(&Value::text("maybe")), "uncoercible values are passed on as text");
        assert_eq!(a.get("unknown"), None);
        assert_eq!(a.get("s"), None, "the desktop session key never reaches the engine");
        let routed = t.route("PATCH", "/v1/chat/conversations/c1").unwrap();
        let a = adapter::build_args(&routed, Some("cid=evil&body=x"), Some(&serde_json::json!({"title": "T"})));
        assert_eq!(a.get("cid"), Some(&Value::text("c1")), "path parameter cannot be overridden by the query");
        assert_eq!(a.get("body").unwrap().get("title"), Some(&Value::text("T")));
        let routed = t.route("PATCH", "/v1/settings").unwrap();
        let a = adapter::build_args(&routed, None, Some(&serde_json::json!({"x": 1})));
        assert_eq!(a.get("patch").unwrap().get("x"), Some(&Value::Int(1)));
        let routed = t.route("GET", "/v1/system").unwrap();
        assert_eq!(adapter::build_args(&routed, None, Some(&serde_json::json!({"ignored": true}))), Value::Map(vec![]));
    }

    #[test]
    fn error_bodies_and_statuses() {
        use crate::snp::error::RemoteError;
        let r = |status, detail| SnpError::Remote(RemoteError { code: "conflict".into(), status, message: "msg".into(), retryable: false, detail });
        assert_eq!(adapter::error_body(&r(409, None)), serde_json::json!({"detail": "msg", "code": "conflict"}));
        let b = adapter::error_body(&r(409, Some(m(vec![("superseded", Value::Bool(true))]))));
        assert_eq!(b["superseded"], true);
        assert_eq!(b["detail"], "msg");
        let b = adapter::error_body(&r(422, Some(Value::Array(vec![m(vec![("msg", Value::text("bad"))])]))));
        assert_eq!(b["detail"][0]["msg"], "bad");
        assert_eq!(SnpError::Unauthenticated(String::new()).http_status(), 401);
        assert_eq!(SnpError::Refused { addr: "x".into() }.http_status(), 502);
        assert_eq!(SnpError::PinMismatch { expected: Pin([0; 32]), presented: None }.http_status(), 502);
        let b = adapter::error_body(&SnpError::ServerProof);
        assert!(b["detail"].as_str().unwrap().contains("server proof") && b["error"] == "server_proof");
        let hello: serde_json::Value = serde_json::from_str(&adapter::hello_text("1.9")).unwrap();
        assert_eq!(hello, serde_json::json!({"type": "hello", "data": {"api_version": "1.9", "heartbeat_s": 15}}));
    }

    #[tokio::test]
    async fn get_maps_query_to_args_and_returns_json() {
        let rig = Rig::new().await;
        let r = rig.req("GET", "/v1/jobs?status=running&limit=5&extra=1").send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers()["content-type"], "application/json");
        assert_eq!(r.headers()["access-control-allow-origin"], "*");
        let v: serde_json::Value = r.json().await.unwrap();
        assert_eq!(v["op"], "job.list");
        assert_eq!(v["args"], serde_json::json!({"status": "running", "limit": 5}));
        assert!(v["idem"].is_null(), "GET carries no idempotency key");
    }

    #[tokio::test]
    async fn writes_carry_a_fresh_idempotency_key_and_the_json_body() {
        let rig = Rig::new().await;
        for i in 0..2 {
            let r = rig.req("PATCH", "/v1/chat/conversations/c%201").json(&serde_json::json!({"title": format!("T{i}")})).send().await.unwrap();
            assert_eq!(r.status(), 200);
            let v: serde_json::Value = r.json().await.unwrap();
            assert_eq!(v["op"], "chat.conversation.update");
            assert_eq!(v["args"]["cid"], "c 1");
            assert_eq!(v["args"]["body"]["title"], format!("T{i}"));
            assert!(v["idem"].as_str().unwrap().len() >= 32);
        }
        let opens = rig.server.knobs.opens_of("chat.conversation.update");
        assert_ne!(opens[0].idem, opens[1].idem, "fresh key per operation");
        let r = rig.req("PATCH", "/v1/settings").json(&serde_json::json!({"a": 1})).send().await.unwrap();
        assert_eq!(r.json::<serde_json::Value>().await.unwrap()["args"]["patch"]["a"], 1);
        let r = rig.req("POST", "/v1/chat/conversations").header("content-type", "application/json").body("{nope").send().await.unwrap();
        assert_eq!(r.status(), 400);
        assert!(r.json::<serde_json::Value>().await.unwrap()["detail"].as_str().unwrap().contains("JSON"));
    }

    #[tokio::test]
    async fn long_running_operations_state_an_explicit_deadline() {
        let rig = Rig::new().await;
        rig.req("POST", "/v1/models/install").json(&serde_json::json!({})).send().await.unwrap();
        assert_eq!(rig.server.knobs.opens_of("model.install")[0].deadline_ms, Some(3_600_000));
        rig.req("GET", "/v1/system").send().await.unwrap();
        assert_eq!(rig.server.knobs.opens_of("system.info")[0].deadline_ms, None);
    }

    #[tokio::test]
    async fn export_text_is_returned_raw() {
        let rig = Rig::new().await;
        rig.server.knobs.script("chat.conversation.export", vec![Reply::End(Value::text("# Title\n\nhello"))]);
        let r = rig.req("GET", "/v1/chat/conversations/c1/export?format=md").send().await.unwrap();
        assert_eq!(r.headers()["content-type"], "text/markdown; charset=utf-8");
        assert_eq!(r.text().await.unwrap(), "# Title\n\nhello");
    }

    #[tokio::test]
    async fn server_errors_keep_status_and_structured_detail() {
        let rig = Rig::new().await;
        rig.server.knobs.script(
            "paintbrush.render",
            vec![Reply::Error { code: "conflict".into(), status: 409, message: "superseded by a newer request".into(), retryable: false, detail: Some(m(vec![("superseded", Value::Bool(true))])) }],
        );
        let r = rig.req("POST", "/v1/experiments/paintbrush/render").json(&serde_json::json!({})).send().await.unwrap();
        assert_eq!(r.status(), 409);
        let v: serde_json::Value = r.json().await.unwrap();
        assert_eq!(v["detail"], "superseded by a newer request");
        assert_eq!(v["superseded"], true);
        // Unknown routes and methods are answered locally, never forwarded anywhere.
        let r = rig.req("GET", "/v1/definitely/not/there").send().await.unwrap();
        assert_eq!(r.status(), 404);
        assert!(r.json::<serde_json::Value>().await.unwrap()["detail"].as_str().unwrap().contains("not available over SNP"));
        let r = rig.http.get(format!("{}/v1/system", rig.base)).send().await.unwrap();
        assert_eq!(r.status(), 403, "the desktop session key is still required");
    }

    fn data_events(body: &str) -> Vec<serde_json::Value> {
        body.split("\n\n").filter(|c| !c.is_empty()).map(|c| serde_json::from_str(c.strip_prefix("data: ").expect("SSE data line")).unwrap()).collect()
    }

    #[tokio::test]
    async fn chat_generate_streams_the_same_sse_bytes_the_webview_parses() {
        let rig = Rig::new().await;
        let evs = [
            m(vec![("type", Value::text("user_message")), ("message", m(vec![("id", Value::text("m1")), ("content", Value::text("hi"))]))]),
            m(vec![("type", Value::text("assistant_start")), ("message", m(vec![("id", Value::text("m2"))]))]),
            m(vec![("type", Value::text("delta")), ("content", Value::text("héllo ✓")), ("reasoning", Value::text(""))]),
            m(vec![("type", Value::text("delta")), ("content", Value::text("")), ("reasoning", Value::text("thinking"))]),
            m(vec![("type", Value::text("done")), ("message", m(vec![("id", Value::text("m2")), ("tokens", Value::Int(7))]))]),
        ];
        let mut replies: Vec<Reply> = evs.iter().cloned().map(Reply::Msg).collect();
        replies.push(Reply::End(Value::Null));
        rig.server.knobs.script("chat.generate", replies);
        let r = rig.req("POST", "/v1/chat/conversations/c1/generate").json(&serde_json::json!({"content": "hi", "model_ref": "x"})).send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers()["content-type"], "text/event-stream");
        assert_eq!(r.headers()["cache-control"], "no-cache");
        let body = r.text().await.unwrap();
        assert!(body.starts_with("data: {") && body.ends_with("}\n\n"));
        assert!(body.contains("héllo ✓"), "UTF-8 is not escaped, like ensure_ascii=False");
        assert_eq!(body.matches("\n\n").count(), 5);
        let parsed = data_events(&body);
        assert_eq!(parsed[2], serde_json::json!({"type": "delta", "content": "héllo ✓", "reasoning": ""}));
        assert_eq!(parsed[3]["reasoning"], "thinking");
        assert_eq!(parsed[4]["type"], "done");
        let open = &rig.server.knobs.opens_of("chat.generate")[0];
        assert_eq!(open.args.get("cid"), Some(&Value::text("c1")));
        assert_eq!(open.args.get("body").unwrap().get("content"), Some(&Value::text("hi")));
        assert!(open.idem.is_none(), "streams carry no idempotency key");
        // The agent stream is converted the same way.
        rig.server.knobs.script("agent.completion", vec![Reply::Msg(m(vec![("type", Value::text("delta")), ("content", Value::text("x"))])), Reply::Msg(m(vec![("type", Value::text("done"))])), Reply::End(Value::Null)]);
        let body = rig.req("POST", "/v1/agent/completions").json(&serde_json::json!({"messages": []})).send().await.unwrap().text().await.unwrap();
        assert_eq!(data_events(&body), vec![serde_json::json!({"type": "delta", "content": "x"}), serde_json::json!({"type": "done"})]);
    }

    #[tokio::test]
    async fn validation_failures_before_the_stream_starts_are_http_errors() {
        let rig = Rig::new().await;
        rig.server.knobs.script("chat.generate", vec![Reply::Error { code: "not_found".into(), status: 404, message: "conversation not found".into(), retryable: false, detail: None }]);
        let r = rig.req("POST", "/v1/chat/conversations/zz/generate").json(&serde_json::json!({})).send().await.unwrap();
        assert_eq!(r.status(), 404);
        assert_eq!(r.json::<serde_json::Value>().await.unwrap()["detail"], "conversation not found");
    }

    #[tokio::test]
    async fn failures_after_the_stream_started_become_error_events() {
        let rig = Rig::new().await;
        rig.server.knobs.script(
            "chat.generate",
            vec![Reply::Msg(m(vec![("type", Value::text("delta")), ("content", Value::text("a"))])), Reply::Error { code: "unavailable".into(), status: 503, message: "model server went away".into(), retryable: true, detail: None }],
        );
        let body = rig.req("POST", "/v1/chat/conversations/c1/generate").json(&serde_json::json!({})).send().await.unwrap().text().await.unwrap();
        let evs = data_events(&body);
        assert_eq!(evs.len(), 2);
        assert_eq!(evs[1], serde_json::json!({"type": "error", "message": "model server went away"}));
    }

    #[tokio::test]
    async fn a_disconnecting_webview_cancels_the_generation() {
        let rig = Rig::new().await;
        rig.server.knobs.script("chat.generate", vec![Reply::Msg(m(vec![("type", Value::text("user_message"))])), Reply::WaitCancel]);
        let mut r = rig.req("POST", "/v1/chat/conversations/c1/generate").json(&serde_json::json!({})).send().await.unwrap();
        let first = r.chunk().await.unwrap().unwrap();
        assert!(String::from_utf8_lossy(&first).contains("user_message"));
        assert!(rig.server.knobs.cancels.lock().unwrap().is_empty());
        drop(r);
        eventually("the server received CANCEL", || !rig.server.knobs.cancels.lock().unwrap().is_empty()).await;
    }

    fn multipart(name: &str, mime: &str, data: &[u8]) -> (String, Vec<u8>) {
        let b = "----sarnessboundary7MA4YWxk";
        let mut body = format!("--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n").into_bytes();
        body.extend_from_slice(data);
        body.extend_from_slice(format!("\r\n--{b}--\r\n").as_bytes());
        (format!("multipart/form-data; boundary={b}"), body)
    }

    #[tokio::test]
    async fn multipart_uploads_stream_as_data_frames() {
        let rig = Rig::new().await;
        rig.server.knobs.limits(|l| {
            l.initial_window = 16_384;
            l.conn_window = 65_536;
        });
        // 3 MiB: far beyond axum's default 2 MB body limit and the SNP windows.
        let data: Vec<u8> = (0..3 * 1024 * 1024).map(|i| (i % 249) as u8).collect();
        let (ct, body) = multipart("pic.png", "image/png", &data);
        let r = rig.req("POST", "/v1/uploads").header("content-type", ct).body(body).send().await.unwrap();
        assert_eq!(r.status(), 200);
        let v: serde_json::Value = r.json().await.unwrap();
        assert_eq!(v["id"], "up_1");
        assert_eq!(v["size"], data.len());
        assert_eq!(v["name"], "pic.png");
        assert_eq!(v["mime"], "image/png");
        assert_eq!(rig.server.knobs.upload_bytes.load(SeqCst), data.len() as u64);
        assert!(!rig.server.knobs.client_overran_credit.load(SeqCst));
        // A request that is not multipart is refused.
        let r = rig.req("POST", "/v1/uploads").json(&serde_json::json!({})).send().await.unwrap();
        assert_eq!(r.status(), 400);
    }

    #[tokio::test]
    async fn an_upload_aborted_by_the_webview_is_cancelled_and_discarded() {
        let rig = Rig::new().await;
        let (ct, body) = multipart("big.bin", "application/octet-stream", &vec![7u8; 200_000]);
        let first = body[..60_000].to_vec();
        let stream = futures_util::stream::iter(vec![Ok::<_, std::io::Error>(Bytes::from(first))]).chain(futures_util::stream::pending());
        let req = rig.req("POST", "/v1/uploads").header("content-type", ct).body(reqwest::Body::wrap_stream(stream));
        let _ = tokio::time::timeout(Duration::from_millis(600), req.send()).await; // dropped mid-body
        eventually("server saw the upload cancelled", || rig.server.knobs.upload_cancelled.load(SeqCst)).await;
        assert!(!rig.server.knobs.upload_done.load(SeqCst));
    }

    #[tokio::test]
    async fn upload_too_large_maps_to_413_and_stops_the_transfer() {
        let rig = Rig::new().await;
        rig.server.knobs.upload_limit.store(100_000, SeqCst);
        let (ct, body) = multipart("x.bin", "application/octet-stream", &vec![1u8; 1_500_000]);
        let r = rig.req("POST", "/v1/uploads").header("content-type", ct).body(body).send().await.unwrap();
        assert_eq!(r.status(), 413);
        assert_eq!(r.json::<serde_json::Value>().await.unwrap()["detail"], "upload too large");
        assert!(!rig.server.knobs.upload_done.load(SeqCst));
        assert!(rig.server.knobs.upload_bytes.load(SeqCst) < 1_500_000);
    }

    #[tokio::test]
    async fn media_comes_from_file_get_is_cached_and_served_with_ranges() {
        let rig = Rig::new().await;
        let url = rig.url("/v1/files/f_1");
        let r = rig.http.get(&url).send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.headers()["content-type"], "image/png");
        let body = r.bytes().await.unwrap();
        let expect: Vec<u8> = (0..300u32).map(|i| (i % 200) as u8).collect();
        assert_eq!(body.as_ref(), &expect[..]);
        assert!(proxy::cached_media_path(rig.dir.path(), "f_1").unwrap().to_string_lossy().ends_with("f_1.png"));
        let r = rig.http.get(&url).header("range", "bytes=10-19").send().await.unwrap();
        assert_eq!(r.status(), 206);
        assert_eq!(r.bytes().await.unwrap().as_ref(), &expect[10..20]);
        assert_eq!(rig.server.knobs.opens_of("file.get").len(), 1, "content-addressed cache: fetched once");
        // Missing files surface the server's 404.
        let r = rig.http.get(rig.url("/v1/files/f_missing")).send().await.unwrap();
        assert_eq!(r.status(), 404);
        assert_eq!(r.json::<serde_json::Value>().await.unwrap()["detail"], "file not found");
        assert!(proxy::cached_media_path(rig.dir.path(), "f_missing").is_none());
        assert!(std::fs::read_dir(rig.dir.path().join("media")).unwrap().flatten().all(|e| !e.file_name().to_string_lossy().ends_with(".part")), "no partial files left behind");
    }

    #[tokio::test]
    async fn file_get_supports_byte_ranges_at_the_protocol_level() {
        let s = fake::start().await;
        let c = client(&s);
        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("part.bin");
        let meta = adapter::download_to_file(&c, "f_9", &dest, Some((100, Some(149)))).await.unwrap();
        assert_eq!((meta.start, meta.end, meta.size, meta.mime.as_str()), (100, 149, 300, "image/png"));
        let expect: Vec<u8> = (100..150u32).map(|i| (i % 200) as u8).collect();
        assert_eq!(std::fs::read(&dest).unwrap(), expect);
        let args = &s.knobs.opens_of("file.get")[0].args;
        assert_eq!(args.get("range").unwrap().get("start"), Some(&Value::Int(100)));
        assert_eq!(args.get("range").unwrap().get("end"), Some(&Value::Int(149)));
    }

    #[tokio::test]
    async fn the_event_websocket_is_bridged_with_the_engines_message_shapes() {
        let rig = Rig::new().await;
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{}/v1/events?s={}", rig.base.trim_start_matches("http://"), rig.shared.session)).await.unwrap();
        let hello: serde_json::Value = serde_json::from_str(ws.next().await.unwrap().unwrap().into_text().unwrap().as_str()).unwrap();
        assert_eq!(hello, serde_json::json!({"type": "hello", "data": {"api_version": "1.9", "heartbeat_s": 15}}));
        eventually("subscribed", || rig.server.knobs.events.receiver_count() == 1).await;
        rig.server.knobs.events.send(m(vec![("type", Value::text("job.updated")), ("ts", Value::Float(12.5)), ("data", m(vec![("id", Value::text("j1")), ("raw", Value::Bytes(vec![1, 2]))]))])).unwrap();
        let ev: serde_json::Value = serde_json::from_str(ws.next().await.unwrap().unwrap().into_text().unwrap().as_str()).unwrap();
        assert_eq!(ev, serde_json::json!({"type": "job.updated", "ts": 12.5, "data": {"id": "j1", "raw": "AQI="}}));
        ws.send(tokio_tungstenite::tungstenite::Message::Text("{\"type\":\"ping\"}".into())).await.unwrap();
        let pong = ws.next().await.unwrap().unwrap().into_text().unwrap();
        assert_eq!(pong.as_str(), "{\"type\":\"pong\"}");
        // Losing the SNP subscription closes the socket so the webview marks the server offline and refreshes.
        rig.server.knobs.kill_all();
        let end = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                match ws.next().await {
                    None | Some(Err(_)) | Some(Ok(tokio_tungstenite::tungstenite::Message::Close(_))) => break,
                    Some(Ok(_)) => continue,
                }
            }
        })
        .await;
        assert!(end.is_ok(), "socket should close when the subscription is lost");
    }

    #[tokio::test]
    async fn auth_failure_is_reported_and_there_is_no_http_fallback() {
        // A plain TCP listener standing in for the engine's HTTP port. Even with an HTTP upstream
        // configured, an SNP profile must never talk to it.
        let http_port = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let hp = http_port.local_addr().unwrap().port();
        let touched = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let t2 = touched.clone();
        tokio::spawn(async move {
            while http_port.accept().await.is_ok() {
                t2.fetch_add(1, SeqCst);
            }
        });
        let server = fake::start().await;
        server.knobs.reject_auth.store(true, SeqCst);
        let rig = Rig::with(server).await;
        *rig.shared.upstream.write().await = Some(Upstream { base: format!("http://127.0.0.1:{hp}"), token: TOKEN.into() });
        for path in ["/v1/system", "/v1/jobs", "/v1/chat/conversations"] {
            let r = rig.req("GET", path).send().await.unwrap();
            assert_eq!(r.status(), 401, "{path}");
            let v: serde_json::Value = r.json().await.unwrap();
            assert!(v["detail"].as_str().unwrap().contains("rejected the API token"));
            assert_eq!(v["error"], "auth");
        }
        let r = rig.req("POST", "/v1/chat/conversations/c/generate").json(&serde_json::json!({})).send().await.unwrap();
        assert_eq!(r.status(), 401);
        let r = rig.http.get(rig.url("/v1/files/f_1")).send().await.unwrap();
        assert_eq!(r.status(), 401);
        let ws = tokio_tungstenite::connect_async(format!("ws://{}/v1/events?s={}", rig.base.trim_start_matches("http://"), rig.shared.session)).await;
        assert!(ws.is_err(), "the event socket is refused too");
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(touched.load(SeqCst), 0, "the HTTP port was never contacted");
        assert_eq!(rig.server.knobs.accepts.load(SeqCst), 1, "and SNP authentication was not retried");
    }

    #[tokio::test]
    async fn pin_mismatch_is_surfaced_to_the_webview_with_both_pins() {
        let server = fake::start().await;
        let dir = tempfile::tempdir().unwrap();
        let shared = ProxyShared::new(dir.path().to_path_buf());
        let port = proxy::serve(shared.clone()).await.unwrap();
        let mut cf = cfg(&server);
        let wrong = Pin::parse(&format!("sha256:{}", "ee".repeat(32))).unwrap();
        cf.pin = Some(wrong);
        *shared.snp.write().await = Some(Arc::new(SnpBackend { client: SnpClient::new(cf) }));
        let r = reqwest::Client::new().get(format!("http://127.0.0.1:{port}/v1/system?s={}", shared.session)).send().await.unwrap();
        assert_eq!(r.status(), 502);
        let v: serde_json::Value = r.json().await.unwrap();
        let d = v["detail"].as_str().unwrap();
        assert!(d.contains(&wrong.to_string()) && d.contains(&server.pin()), "{d}");
        assert_eq!(v["error"], "pin_mismatch");
    }
}

// ===========================================================================
// Interoperability with the real engine (not run by default)
//
//   SNP_INTEROP_HOST=127.0.0.1 SNP_INTEROP_PORT=18791 SNP_INTEROP_PIN=sha256:... \
//   SNP_INTEROP_TOKEN=sk-... cargo test interop -- --ignored --nocapture
// ===========================================================================
mod interop {
    use super::*;
    use crate::proxy::{self, ProxyShared};
    use crate::snp::adapter::{self, SnpBackend};

    // A valid 1x1 PNG.
    const PNG: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8, 0xcf, 0xc0, 0xf0,
        0x1f, 0x00, 0x05, 0x00, 0x01, 0xff, 0x89, 0x99, 0x3d, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ];

    fn env() -> Option<(String, u16, String, String)> {
        Some((
            std::env::var("SNP_INTEROP_HOST").ok()?,
            std::env::var("SNP_INTEROP_PORT").ok()?.parse().ok()?,
            std::env::var("SNP_INTEROP_PIN").ok()?,
            std::env::var("SNP_INTEROP_TOKEN").ok()?,
        ))
    }

    #[tokio::test]
    #[ignore = "needs a running engine: set SNP_INTEROP_HOST / _PORT / _PIN / _TOKEN"]
    async fn the_client_interoperates_with_the_real_engine() {
        let Some((host, port, pin, token)) = env() else { panic!("SNP_INTEROP_* variables are not set") };
        let cfg = SnpConfig::new(&host, port, Some(Pin::parse(&pin).unwrap()), &token);
        let c = SnpClient::new(cfg.clone());
        let info = c.connect().await.expect("handshake");
        println!("negotiated: version {} server {:?} features {:?} limits {:?} api {} rtt {} ms", info.version, info.server, info.features, info.limits, info.api_version, info.latency_ms);
        assert_eq!(info.pin, pin);

        // TOFU probe sees the same identity and sends nothing.
        assert_eq!(super::super::probe(&host, port, None).await.unwrap().pin, pin);

        // Unary reads and writes.
        let health = c.call("system.health", none(), CallOpts::new(Retry::Safe)).await.unwrap();
        assert_eq!(health.get("service").and_then(Value::as_str), Some("sarness-engine"));
        let sys = c.call("system.info", none(), CallOpts::new(Retry::Safe)).await.unwrap();
        assert!(sys.get("hardware").is_some());
        let conv = c.call("chat.conversation.create", args(vec![("body", m(vec![("title", Value::text("interop test"))]))]), CallOpts::new(Retry::Idem)).await.unwrap();
        let cid = conv.get("id").and_then(Value::as_str).expect("conversation id").to_string();
        let got = c.call("chat.conversation.get", args(vec![("cid", Value::text(cid.clone()))]), CallOpts::new(Retry::Safe)).await.unwrap();
        assert_eq!(got.get("id").and_then(Value::as_str), Some(cid.as_str()));
        let list = c.call("chat.conversation.list", none(), CallOpts::new(Retry::Safe)).await.unwrap();
        assert!(format!("{list:?}").contains(&cid));
        // Typed query parameters are accepted as the adapter sends them.
        c.call("job.list", args(vec![("limit", Value::Int(5)), ("status", Value::text("running"))]), CallOpts::new(Retry::Safe)).await.unwrap();
        c.call("generation.list", args(vec![("limit", Value::Int(3)), ("favorite", Value::Bool(false)), ("since", Value::Float(1.5))]), CallOpts::new(Retry::Safe)).await.unwrap();

        // Errors keep their HTTP-compatible status.
        match c.call("chat.conversation.get", args(vec![("cid", Value::text("does-not-exist"))]), CallOpts::new(Retry::Safe)).await {
            Err(SnpError::Remote(r)) => assert_eq!((r.code.as_str(), r.status), ("not_found", 404)),
            other => panic!("{other:?}"),
        }
        match c.call("no.such.op", none(), CallOpts::new(Retry::Safe)).await {
            Err(SnpError::Remote(r)) => assert_eq!(r.code, "unknown_op"),
            other => panic!("{other:?}"),
        }

        // Upload, then download (whole and ranged) through file.get.
        let mut up = c.open("file.upload", args(vec![("name", Value::text("p.png")), ("mime", Value::text("image/png")), ("size", Value::Int(PNG.len() as i64))])).await.unwrap();
        up.send_data(Bytes::from_static(PNG)).await.unwrap();
        up.end().unwrap();
        let (_, _, rec) = drain_to_end(&mut up).await;
        println!("upload record: {rec:?}");
        let fid = rec.get("id").and_then(Value::as_str).expect("upload id").to_string();
        let dir = tempfile::tempdir().unwrap();
        let whole = dir.path().join("whole");
        let meta = adapter::download_to_file(&c, &fid, &whole, None).await.unwrap();
        assert_eq!(std::fs::read(&whole).unwrap(), PNG);
        assert_eq!(meta.mime, "image/png");
        let part = dir.path().join("part");
        let meta = adapter::download_to_file(&c, &fid, &part, Some((1, Some(8)))).await.unwrap();
        assert_eq!((meta.start, meta.end), (1, 8));
        assert_eq!(std::fs::read(&part).unwrap(), &PNG[1..=8]);

        // A multi-megabyte transfer exercises flow control in both directions against the real engine.
        let big: Vec<u8> = (0..6 * 1024 * 1024u32).map(|i| b'a' + (i % 23) as u8).collect();
        let t0 = Instant::now();
        let mut up = c.open("file.upload", args(vec![("name", Value::text("big.txt")), ("mime", Value::text("text/plain"))])).await.unwrap();
        up.send_data(Bytes::from(big.clone())).await.unwrap();
        up.end().unwrap();
        let (_, _, rec) = drain_to_end(&mut up).await;
        assert_eq!(rec.get("size").and_then(Value::as_u64), Some(big.len() as u64));
        let t_up = t0.elapsed();
        let bigid = rec.get("id").and_then(Value::as_str).unwrap().to_string();
        let dest = dir.path().join("big");
        let t1 = Instant::now();
        adapter::download_to_file(&c, &bigid, &dest, None).await.unwrap();
        assert!(std::fs::read(&dest).unwrap() == big, "downloaded bytes differ");
        println!("6 MiB upload {:?}, download {:?}", t_up, t1.elapsed());

        // Events: hello, then whatever telemetry arrives.
        let mut sub = c.subscribe_events(vec![], true).await.unwrap();
        match tokio::time::timeout(Duration::from_secs(5), sub.next()).await.unwrap() {
            SubEvent::Event(v) => assert_eq!(v.get("type").and_then(Value::as_str), Some("hello")),
            other => panic!("{other:?}"),
        }
        if let Ok(SubEvent::Event(v)) = tokio::time::timeout(Duration::from_secs(8), sub.next()).await {
            println!("first event: {:?}", v.get("type"));
        }
        drop(sub);

        // Cleanup and a cancelled stream.
        c.call("chat.conversation.delete", args(vec![("cid", Value::text(cid))]), CallOpts::new(Retry::Idem)).await.unwrap();
        let st = c.open("event.subscribe", none()).await.unwrap();
        drop(st);
        assert!(c.call("system.health", none(), CallOpts::new(Retry::Safe)).await.is_ok());
        assert_eq!(c.connect_count(), 1);

        // Wrong token and wrong pin are refused with the right errors.
        let bad = SnpClient::new(SnpConfig::new(&host, port, Some(Pin::parse(&pin).unwrap()), "sk-definitely-wrong"));
        assert!(matches!(bad.connect().await, Err(SnpError::Unauthenticated(_))));
        let wrong_pin = SnpClient::new(SnpConfig::new(&host, port, Some(Pin::parse(&format!("sha256:{}", "00".repeat(32))).unwrap()), &token));
        assert!(matches!(wrong_pin.connect().await, Err(SnpError::PinMismatch { .. })));

        // And the whole thing through the loopback proxy, as the webview would use it.
        let dir = tempfile::tempdir().unwrap();
        let shared = ProxyShared::new(dir.path().to_path_buf());
        let pport = proxy::serve(shared.clone()).await.unwrap();
        *shared.snp.write().await = Some(Arc::new(SnpBackend { client: SnpClient::new(cfg) }));
        let http = reqwest::Client::new();
        let base = format!("http://127.0.0.1:{pport}");
        let s = &shared.session;
        let v: serde_json::Value = http.get(format!("{base}/v1/system?s={s}")).send().await.unwrap().json().await.unwrap();
        assert!(v.get("hardware").is_some(), "{v}");
        let v: serde_json::Value = http.post(format!("{base}/v1/chat/conversations?s={s}")).json(&serde_json::json!({"title": "via proxy"})).send().await.unwrap().json().await.unwrap();
        let pcid = v["id"].as_str().unwrap().to_string();
        let r = http.get(format!("{base}/v1/chat/conversations/{pcid}/export?format=md&s={s}")).send().await.unwrap();
        println!("export md: {} {:?}", r.status(), r.headers().get("content-type"));
        assert_eq!(r.status(), 200);
        let r = http.get(format!("{base}/v1/chat/conversations/nope?s={s}")).send().await.unwrap();
        assert_eq!(r.status(), 404);
        println!("detail: {}", r.text().await.unwrap());
        // Generation without a usable model fails before the stream starts: an ordinary HTTP error.
        let r = http.post(format!("{base}/v1/chat/conversations/{pcid}/generate?s={s}")).json(&serde_json::json!({"content": "hi"})).send().await.unwrap();
        let (status, text) = (r.status(), r.text().await.unwrap());
        println!("generate without a model: {status} {text}");
        assert!(status.is_client_error() || status.is_server_error(), "{status} {text}");
        assert!(serde_json::from_str::<serde_json::Value>(&text).unwrap()["detail"].is_string());
        let r = http.delete(format!("{base}/v1/chat/conversations/{pcid}?s={s}")).send().await.unwrap();
        assert_eq!(r.status(), 200);
        let boundary = "xBOUNDARYx";
        let mut body = format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"p.png\"\r\nContent-Type: image/png\r\n\r\n").into_bytes();
        body.extend_from_slice(PNG);
        body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
        let r = http.post(format!("{base}/v1/uploads?s={s}")).header("content-type", format!("multipart/form-data; boundary={boundary}")).body(body).send().await.unwrap();
        assert_eq!(r.status(), 200);
        let rec: serde_json::Value = r.json().await.unwrap();
        let r = http.get(format!("{base}/v1/files/{}?s={s}", rec["id"].as_str().unwrap())).send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.bytes().await.unwrap().as_ref(), PNG);
    }
}
