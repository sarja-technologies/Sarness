//! SNP-CBOR (SNP.md §4.2): a strict, hand-written CBOR profile.
//!
//! The decoder validates every declared length against the remaining input
//! *before* allocating, enforces shortest-form integers, definite lengths, unique
//! text keys, valid UTF-8, no tags / `undefined` / non-finite floats, a nesting
//! limit of 32 and a limit of 100 000 data items per payload.

use std::collections::HashSet;

pub const MAX_DEPTH: usize = 32;
pub const MAX_ITEMS: usize = 100_000;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Array(Vec<Value>),
    /// Insertion-ordered; keys are unique (enforced by the codec).
    Map(Vec<(String, Value)>),
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CborError {
    #[error("truncated input")]
    Truncated,
    #[error("trailing data after the top-level item")]
    Trailing,
    #[error("CBOR tags (major type 6) are not allowed")]
    Tag,
    #[error("indefinite-length items are not allowed")]
    Indefinite,
    #[error("integer or length is not in its shortest encoding")]
    NonShortest,
    #[error("integer outside [-2^63, 2^63-1]")]
    IntRange,
    #[error("forbidden simple value")]
    Simple,
    #[error("reserved additional information value")]
    Reserved,
    #[error("NaN and infinities are not allowed")]
    NonFinite,
    #[error("map key is not a text string")]
    KeyType,
    #[error("duplicate map key")]
    DuplicateKey,
    #[error("text string is not valid UTF-8")]
    Utf8,
    #[error("nesting deeper than {MAX_DEPTH}")]
    Depth,
    #[error("more than {MAX_ITEMS} data items")]
    Items,
    #[error("declared length exceeds the remaining input")]
    Length,
}

impl Value {
    pub fn map<K: Into<String>, I: IntoIterator<Item = (K, Value)>>(items: I) -> Value {
        Value::Map(items.into_iter().map(|(k, v)| (k.into(), v)).collect())
    }
    pub fn text(s: impl Into<String>) -> Value {
        Value::Text(s.into())
    }
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Map(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Text(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Int(i) if *i >= 0 => Some(*i as u64),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Value::Bytes(b) => Some(b),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(a) => Some(a),
            _ => None,
        }
    }
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
}

// ------------------------------------------------------------------ decoding
struct Decoder<'a> {
    buf: &'a [u8],
    pos: usize,
    items: usize,
}

pub fn decode(buf: &[u8]) -> Result<Value, CborError> {
    let mut d = Decoder { buf, pos: 0, items: 0 };
    let v = d.item(0)?;
    if d.pos != buf.len() {
        return Err(CborError::Trailing);
    }
    Ok(v)
}

impl<'a> Decoder<'a> {
    fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], CborError> {
        if n > self.remaining() {
            return Err(CborError::Truncated);
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    /// Reads the argument of an initial byte (additional information `ai`).
    fn arg(&mut self, ai: u8) -> Result<u64, CborError> {
        match ai {
            0..=23 => Ok(ai as u64),
            24 => {
                let v = self.take(1)?[0] as u64;
                if v < 24 { Err(CborError::NonShortest) } else { Ok(v) }
            }
            25 => {
                let b = self.take(2)?;
                let v = u16::from_be_bytes([b[0], b[1]]) as u64;
                if v < 0x100 { Err(CborError::NonShortest) } else { Ok(v) }
            }
            26 => {
                let b = self.take(4)?;
                let v = u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as u64;
                if v < 0x1_0000 { Err(CborError::NonShortest) } else { Ok(v) }
            }
            27 => {
                let b = self.take(8)?;
                let v = u64::from_be_bytes(b.try_into().unwrap());
                if v < 0x1_0000_0000 { Err(CborError::NonShortest) } else { Ok(v) }
            }
            28..=30 => Err(CborError::Reserved),
            _ => Err(CborError::Indefinite),
        }
    }

    fn count_item(&mut self) -> Result<(), CborError> {
        self.items += 1;
        if self.items > MAX_ITEMS {
            return Err(CborError::Items);
        }
        Ok(())
    }

    fn item(&mut self, depth: usize) -> Result<Value, CborError> {
        self.count_item()?;
        let ib = self.take(1)?[0];
        let (major, ai) = (ib >> 5, ib & 31);
        match major {
            0 => {
                let n = self.arg(ai)?;
                i64::try_from(n).map(Value::Int).map_err(|_| CborError::IntRange)
            }
            1 => {
                let n = self.arg(ai)?;
                let n = i64::try_from(n).map_err(|_| CborError::IntRange)?;
                Ok(Value::Int(-1 - n))
            }
            2 | 3 => {
                let len = self.arg(ai)?;
                if len > self.remaining() as u64 {
                    return Err(CborError::Length);
                }
                let s = self.take(len as usize)?;
                if major == 2 {
                    Ok(Value::Bytes(s.to_vec()))
                } else {
                    std::str::from_utf8(s).map(|t| Value::Text(t.to_owned())).map_err(|_| CborError::Utf8)
                }
            }
            4 => {
                let n = self.arg(ai)?;
                // Every element needs at least one octet.
                if n > self.remaining() as u64 {
                    return Err(CborError::Length);
                }
                if self.items as u64 + n > MAX_ITEMS as u64 {
                    return Err(CborError::Items);
                }
                if depth + 1 > MAX_DEPTH {
                    return Err(CborError::Depth);
                }
                let mut out = Vec::with_capacity(n as usize);
                for _ in 0..n {
                    out.push(self.item(depth + 1)?);
                }
                Ok(Value::Array(out))
            }
            5 => {
                let n = self.arg(ai)?;
                if n.checked_mul(2).map(|x| x > self.remaining() as u64).unwrap_or(true) {
                    return Err(CborError::Length);
                }
                if self.items as u64 + 2 * n > MAX_ITEMS as u64 {
                    return Err(CborError::Items);
                }
                if depth + 1 > MAX_DEPTH {
                    return Err(CborError::Depth);
                }
                let mut out: Vec<(String, Value)> = Vec::with_capacity(n as usize);
                let mut seen: HashSet<String> = HashSet::with_capacity(n as usize);
                for _ in 0..n {
                    let k = match self.item(depth + 1)? {
                        Value::Text(t) => t,
                        _ => return Err(CborError::KeyType),
                    };
                    if !seen.insert(k.clone()) {
                        return Err(CborError::DuplicateKey);
                    }
                    let v = self.item(depth + 1)?;
                    out.push((k, v));
                }
                Ok(Value::Map(out))
            }
            6 => Err(CborError::Tag),
            _ => match ai {
                20 => Ok(Value::Bool(false)),
                21 => Ok(Value::Bool(true)),
                22 => Ok(Value::Null),
                25 => {
                    let b = self.take(2)?;
                    finite(f16_to_f64(u16::from_be_bytes([b[0], b[1]])))
                }
                26 => {
                    let b = self.take(4)?;
                    finite(f32::from_be_bytes([b[0], b[1], b[2], b[3]]) as f64)
                }
                27 => {
                    let b = self.take(8)?;
                    finite(f64::from_be_bytes(b.try_into().unwrap()))
                }
                28..=30 => Err(CborError::Reserved),
                31 => Err(CborError::Indefinite),
                _ => Err(CborError::Simple), // 0..=19 unassigned, 23 undefined, 24 one-byte simple
            },
        }
    }
}

fn finite(f: f64) -> Result<Value, CborError> {
    if f.is_finite() { Ok(Value::Float(f)) } else { Err(CborError::NonFinite) }
}

fn f16_to_f64(h: u16) -> f64 {
    let sign = if h & 0x8000 != 0 { -1.0 } else { 1.0 };
    let exp = ((h >> 10) & 0x1f) as i32;
    let man = (h & 0x3ff) as f64;
    match exp {
        0 => sign * man * 2f64.powi(-24),
        31 => {
            if man == 0.0 { sign * f64::INFINITY } else { f64::NAN }
        }
        _ => sign * (1024.0 + man) * 2f64.powi(exp - 25),
    }
}

/// Exact f16 representation of `x`, if one exists.
fn f64_to_f16_exact(x: f64) -> Option<u16> {
    if x == 0.0 {
        return Some(if x.is_sign_negative() { 0x8000 } else { 0 });
    }
    let sign: u16 = if x < 0.0 { 0x8000 } else { 0 };
    let a = x.abs();
    let bits = a.to_bits();
    let exp = ((bits >> 52) & 0x7ff) as i32 - 1023;
    let man = bits & ((1u64 << 52) - 1);
    if (-14..=15).contains(&exp) {
        if man & ((1u64 << 42) - 1) != 0 {
            return None;
        }
        return Some(sign | (((exp + 15) as u16) << 10) | (man >> 42) as u16);
    }
    if (-24..-14).contains(&exp) {
        let scaled = a * 2f64.powi(24);
        if scaled.fract() == 0.0 && (1.0..1024.0).contains(&scaled) {
            return Some(sign | scaled as u16);
        }
    }
    None
}

// ------------------------------------------------------------------ encoding
fn head(out: &mut Vec<u8>, major: u8, n: u64) {
    let m = major << 5;
    if n < 24 {
        out.push(m | n as u8);
    } else if n < 0x100 {
        out.push(m | 24);
        out.push(n as u8);
    } else if n < 0x1_0000 {
        out.push(m | 25);
        out.extend_from_slice(&(n as u16).to_be_bytes());
    } else if n < 0x1_0000_0000 {
        out.push(m | 26);
        out.extend_from_slice(&(n as u32).to_be_bytes());
    } else {
        out.push(m | 27);
        out.extend_from_slice(&n.to_be_bytes());
    }
}

/// Encodes `v` in the preferred (shortest) serialization with map keys in
/// bytewise-lexicographic order of their encodings.
pub fn encode(v: &Value) -> Result<Vec<u8>, CborError> {
    let mut out = Vec::new();
    enc(v, &mut out, 0)?;
    Ok(out)
}

fn enc(v: &Value, out: &mut Vec<u8>, depth: usize) -> Result<(), CborError> {
    match v {
        Value::Null => out.push(0xf6),
        Value::Bool(b) => out.push(if *b { 0xf5 } else { 0xf4 }),
        Value::Int(i) => {
            if *i >= 0 {
                head(out, 0, *i as u64);
            } else {
                head(out, 1, !(*i) as u64);
            }
        }
        Value::Float(f) => {
            if !f.is_finite() {
                return Err(CborError::NonFinite);
            }
            if let Some(h) = f64_to_f16_exact(*f) {
                out.push(0xf9);
                out.extend_from_slice(&h.to_be_bytes());
            } else if (*f as f32) as f64 == *f {
                out.push(0xfa);
                out.extend_from_slice(&(*f as f32).to_be_bytes());
            } else {
                out.push(0xfb);
                out.extend_from_slice(&f.to_be_bytes());
            }
        }
        Value::Text(s) => {
            head(out, 3, s.len() as u64);
            out.extend_from_slice(s.as_bytes());
        }
        Value::Bytes(b) => {
            head(out, 2, b.len() as u64);
            out.extend_from_slice(b);
        }
        Value::Array(a) => {
            if depth + 1 > MAX_DEPTH {
                return Err(CborError::Depth);
            }
            head(out, 4, a.len() as u64);
            for x in a {
                enc(x, out, depth + 1)?;
            }
        }
        Value::Map(m) => {
            if depth + 1 > MAX_DEPTH {
                return Err(CborError::Depth);
            }
            let mut keyed: Vec<(Vec<u8>, &Value)> = Vec::with_capacity(m.len());
            for (k, val) in m {
                let mut kb = Vec::with_capacity(k.len() + 2);
                head(&mut kb, 3, k.len() as u64);
                kb.extend_from_slice(k.as_bytes());
                keyed.push((kb, val));
            }
            keyed.sort_by(|a, b| a.0.cmp(&b.0));
            if keyed.windows(2).any(|w| w[0].0 == w[1].0) {
                return Err(CborError::DuplicateKey);
            }
            head(out, 5, m.len() as u64);
            for (kb, val) in keyed {
                out.extend_from_slice(&kb);
                enc(val, out, depth + 1)?;
            }
        }
    }
    Ok(())
}

// ------------------------------------------------------------- JSON bridging
/// JSON → CBOR data model. Integers beyond i64 become floats.
pub fn from_json(v: &serde_json::Value) -> Value {
    use serde_json::Value as J;
    match v {
        J::Null => Value::Null,
        J::Bool(b) => Value::Bool(*b),
        J::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::Int(i)
            } else {
                Value::Float(n.as_f64().unwrap_or(0.0))
            }
        }
        J::String(s) => Value::Text(s.clone()),
        J::Array(a) => Value::Array(a.iter().map(from_json).collect()),
        J::Object(o) => Value::Map(o.iter().map(|(k, v)| (k.clone(), from_json(v))).collect()),
    }
}

/// CBOR → JSON. Byte strings become base64 text (only the loopback adapter does this).
pub fn to_json(v: &Value) -> serde_json::Value {
    use base64::Engine;
    use serde_json::Value as J;
    match v {
        Value::Null => J::Null,
        Value::Bool(b) => J::Bool(*b),
        Value::Int(i) => J::from(*i),
        Value::Float(f) => serde_json::Number::from_f64(*f).map(J::Number).unwrap_or(J::Null),
        Value::Text(s) => J::String(s.clone()),
        Value::Bytes(b) => J::String(base64::engine::general_purpose::STANDARD.encode(b)),
        Value::Array(a) => J::Array(a.iter().map(to_json).collect()),
        Value::Map(m) => J::Object(m.iter().map(|(k, v)| (k.clone(), to_json(v))).collect()),
    }
}
