//! SNP framing (SNP.md §4): 12-octet header followed by `length` payload octets.
//!
//! The length is validated from the header alone, before any payload octet is
//! buffered, so a hostile peer cannot make us allocate more than the applicable limit.

use bytes::{Buf, Bytes, BytesMut};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAGIC: [u8; 2] = [0x53, 0x4E];
pub const HEADER_LEN: usize = 12;
/// Payload limit before AUTH_OK (§4).
pub const PRE_AUTH_MAX: u32 = 4096;

pub mod ftype {
    pub const HELLO: u8 = 0x01;
    pub const HELLO_OK: u8 = 0x02;
    pub const AUTH: u8 = 0x03;
    pub const AUTH_OK: u8 = 0x04;
    pub const PING: u8 = 0x05;
    pub const PONG: u8 = 0x06;
    pub const GOAWAY: u8 = 0x07;
    pub const OPEN: u8 = 0x10;
    pub const MSG: u8 = 0x11;
    pub const DATA: u8 = 0x12;
    pub const END: u8 = 0x13;
    pub const ERROR: u8 = 0x14;
    pub const CANCEL: u8 = 0x15;
    pub const WINDOW: u8 = 0x16;

    pub fn is_known(t: u8) -> bool {
        matches!(t, 0x01..=0x07 | 0x10..=0x16)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub ftype: u8,
    pub stream: u32,
    pub payload: Bytes,
}

impl Frame {
    pub fn new(ftype: u8, stream: u32, payload: impl Into<Bytes>) -> Self {
        Frame { ftype, stream, payload: payload.into() }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(HEADER_LEN + self.payload.len());
        out.extend_from_slice(&MAGIC);
        out.push(self.ftype);
        out.push(0);
        out.extend_from_slice(&self.stream.to_be_bytes());
        out.extend_from_slice(&(self.payload.len() as u32).to_be_bytes());
        out.extend_from_slice(&self.payload);
        out
    }
}

#[derive(Debug, thiserror::Error)]
pub enum FrameError {
    #[error("bad magic {0:02x}{1:02x}")]
    BadMagic(u8, u8),
    #[error("non-zero flags 0x{0:02x}")]
    BadFlags(u8),
    #[error("unknown frame type 0x{0:02x}")]
    UnknownType(u8),
    #[error("frame payload of {len} octets exceeds the limit of {max}")]
    TooLarge { len: u32, max: u32 },
    #[error("connection closed")]
    Closed,
    #[error("connection closed in the middle of a frame")]
    Truncated,
    #[error("i/o error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub ftype: u8,
    pub stream: u32,
    pub len: u32,
}

/// Validates a header. Performs no allocation.
pub fn parse_header(h: &[u8], max: u32) -> Result<Header, FrameError> {
    debug_assert!(h.len() >= HEADER_LEN);
    if h[0] != MAGIC[0] || h[1] != MAGIC[1] {
        return Err(FrameError::BadMagic(h[0], h[1]));
    }
    if h[3] != 0 {
        return Err(FrameError::BadFlags(h[3]));
    }
    if !ftype::is_known(h[2]) {
        return Err(FrameError::UnknownType(h[2]));
    }
    let stream = u32::from_be_bytes([h[4], h[5], h[6], h[7]]);
    let len = u32::from_be_bytes([h[8], h[9], h[10], h[11]]);
    if len > max {
        return Err(FrameError::TooLarge { len, max });
    }
    Ok(Header { ftype: h[2], stream, len })
}

/// Incremental parse from a buffer: `Ok(None)` when more octets are needed.
pub fn try_parse(buf: &mut BytesMut, max: u32) -> Result<Option<Frame>, FrameError> {
    if buf.len() < HEADER_LEN {
        return Ok(None);
    }
    let h = parse_header(&buf[..HEADER_LEN], max)?;
    let total = HEADER_LEN + h.len as usize;
    if buf.len() < total {
        return Ok(None);
    }
    buf.advance(HEADER_LEN);
    let payload = buf.split_to(h.len as usize).freeze();
    Ok(Some(Frame { ftype: h.ftype, stream: h.stream, payload }))
}

/// Cancel-safe frame reader (state lives in `buf`, so a dropped `next()` future loses nothing).
pub struct FrameReader<R> {
    rd: R,
    buf: BytesMut,
    max: u32,
}

impl<R: AsyncRead + Unpin> FrameReader<R> {
    pub fn new(rd: R, max: u32) -> Self {
        FrameReader { rd, buf: BytesMut::with_capacity(8192), max }
    }

    pub fn set_max(&mut self, max: u32) {
        self.max = max;
    }

    pub async fn next(&mut self) -> Result<Frame, FrameError> {
        loop {
            if let Some(f) = try_parse(&mut self.buf, self.max)? {
                return Ok(f);
            }
            // Reserve exactly what the (already validated) frame still needs, at least 8 KiB.
            let need = if self.buf.len() >= HEADER_LEN {
                let len = u32::from_be_bytes([self.buf[8], self.buf[9], self.buf[10], self.buf[11]]) as usize;
                (HEADER_LEN + len).saturating_sub(self.buf.len())
            } else {
                HEADER_LEN - self.buf.len()
            };
            self.buf.reserve(need.max(8192).min(HEADER_LEN + self.max as usize));
            let n = self.rd.read_buf(&mut self.buf).await?;
            if n == 0 {
                return Err(if self.buf.is_empty() { FrameError::Closed } else { FrameError::Truncated });
            }
        }
    }
}

pub async fn write_frame<W: AsyncWrite + Unpin>(w: &mut W, f: &Frame) -> std::io::Result<()> {
    w.write_all(&f.encode()).await?;
    w.flush().await
}
