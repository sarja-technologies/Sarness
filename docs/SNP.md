# SNP/1 — Sarness Native Protocol, version 1

Status: normative. Applies to Sarness Engine ≥ 0.4.0 and Sarness Desktop ≥ 0.2.0.

The key words MUST, MUST NOT, REQUIRED, SHALL, SHOULD, SHOULD NOT, MAY and RECOMMENDED are to be interpreted as in RFC 2119 / RFC 8174 when, and only when, they appear in capitals.

## 1. Scope

SNP is a binary, connection-oriented, multiplexed application protocol between one *client* (the native backend of Sarness Desktop) and one *server* (Sarness Engine). It carries typed operations, server-streamed events, bulk byte transfers and event subscriptions over a single TLS 1.3 connection. It is independent of HTTP: no HTTP semantics (methods, paths, status lines, headers, chunking, Upgrade) appear on the wire. The HTTP/JSON API of the engine is a separate, unaltered interface; SNP does not depend on it and neither interface is implemented in terms of the other. The correspondence between operations and HTTP routes in `snp/ops.json` is informative and exists for the desktop's loopback adapter only.

SNP does not rely on obscurity. All confidentiality and integrity are provided by TLS 1.3; client authentication is a challenge–response keyed by a pre-shared secret; server authentication is by certificate pinning plus a server proof (§5).

## 2. Conventions

* All multi-byte integers are unsigned, big-endian, unless stated.
* `uN` denotes an N-bit unsigned integer. `text` is valid UTF-8 (NUL is permitted). `bytes` is an octet string.
* Maps in this document use text keys. Unless stated otherwise, a receiver MUST ignore unknown map keys and MUST reject a missing REQUIRED key with `bad_request` (stream) or `protocol_error` (connection).

## 3. Transport

| Parameter | Value |
|---|---|
| Underlying transport | TCP |
| Default port | 8744 |
| Security | TLS 1.3 exclusively (TLS ≤ 1.2 MUST be refused) |
| ALPN | `snp/1` (REQUIRED; a peer that does not negotiate it MUST close the connection) |
| Cipher suites | those mandated by TLS 1.3 implementations in use; no application-level selection |
| Server certificate | Leaf certificate with ECDSA P-256 or Ed25519 key. Self-signed certificates are permitted when pinned (§5.1). |
| Client certificate | Not used in SNP/1. |
| Compression | None at TLS or SNP level. |

The TCP connection SHOULD set TCP_NODELAY and keepalive. A server MAY listen on several addresses. A client MAY reach the server through any byte-transparent tunnel (e.g. an SSH local forward); TLS and authentication are still performed end-to-end.

## 4. Framing

Every SNP message is one frame:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+---------------+---------------+---------------+---------------+
|  magic 0x53   |  magic 0x4E   |     type      |     flags     |
+---------------+---------------+---------------+---------------+
|                          stream id (u32)                      |
+---------------------------------------------------------------+
|                         payload length (u32)                  |
+---------------------------------------------------------------+
|                         payload (length octets)               |
```

* `magic` MUST be `53 4E` ("SN"). Any other value is a `protocol_error`.
* `flags` MUST be 0 in SNP/1. A non-zero value is a `protocol_error`.
* `stream id` 0 designates the connection. Streams initiated by the client use odd identifiers that MUST increase strictly monotonically over the life of the connection. Even identifiers are reserved; their use is a `protocol_error`. A stream id is never reused.
* `payload length` MUST NOT exceed the applicable limit: 4096 before `AUTH_OK` has been sent/received, afterwards the negotiated `max_frame` (§6.2). The receiver MUST validate the length from the header and MUST NOT allocate or read the payload before doing so. An oversized frame is a `protocol_error`: send `GOAWAY(frame_too_large)` and close.
* Receivers MUST read exactly `length` payload octets. A frame whose payload fails to parse per its type definition is a `protocol_error` unless the type says otherwise.

### 4.1 Frame types

| Type | Name | Direction | Stream | Payload |
|---|---|---|---|---|
| 0x01 | HELLO | C→S | 0 | CBOR map |
| 0x02 | HELLO_OK | S→C | 0 | CBOR map |
| 0x03 | AUTH | C→S | 0 | CBOR map |
| 0x04 | AUTH_OK | S→C | 0 | CBOR map |
| 0x05 | PING | both | 0 | exactly 8 octets, opaque |
| 0x06 | PONG | both | 0 | the 8 octets of the PING being answered |
| 0x07 | GOAWAY | both | 0 | CBOR map |
| 0x10 | OPEN | C→S | n | CBOR map |
| 0x11 | MSG | both | n | CBOR value (map) |
| 0x12 | DATA | both | n | raw octets |
| 0x13 | END | both | n | CBOR map (may be empty payload = `{}`) |
| 0x14 | ERROR | S→C | n (or 0) | CBOR map |
| 0x15 | CANCEL | C→S | n | CBOR map (may be empty payload = `{}`) |
| 0x16 | WINDOW | both | 0 or n | CBOR map |

An unknown type is a `protocol_error`. (Extensions are introduced only by a new ALPN token or by a feature negotiated in HELLO that explicitly defines new types.)

### 4.2 CBOR profile ("SNP-CBOR")

Structured payloads use CBOR (RFC 8949) restricted as follows. CBOR was chosen over MessagePack and Protobuf because it is an IETF standard with a defined deterministic profile, is self-describing (no code generation, so engine and desktop evolve independently), carries a native byte-string type (needed for nonces, proofs and previews), and has mature, independently written decoders for Python and Rust. The hand-written strict decoders in the reference implementations exist so that all bounds are enforced *before* allocation.

* Permitted major types: 0 (unsigned), 1 (negative), 2 (byte string), 3 (text string), 4 (array), 5 (map), 7 (simple/float: `false`, `true`, `null`, and float16/32/64).
* Integers MUST lie in [−2^63, 2^63−1]. Larger values are rejected.
* Major type 6 (tags) MUST NOT occur. `undefined` and other simple values MUST NOT occur.
* All lengths MUST be definite. Indefinite-length items (additional information 31) MUST be rejected.
* Integer and length arguments MUST use the shortest encoding (preferred serialization). Non-shortest encodings MUST be rejected.
* Map keys MUST be text strings and MUST be unique within a map. Encoders SHOULD emit keys in bytewise-lexicographic order of their encodings; decoders MUST NOT require it.
* Text strings MUST be valid UTF-8.
* Floats: encoders SHOULD use the shortest float width that round-trips; NaN and ±Infinity MUST NOT be encoded.
* Decoder limits (violation → `bad_request` on a stream, `protocol_error` pre-stream): nesting depth ≤ 32; total data items ≤ 100 000 per payload; a declared string/array/map length MUST be validated against the remaining payload octets before allocation (array/map of n items requires at least n, respectively 2n, remaining octets); trailing octets after the single top-level item are rejected.

### 4.3 Data model of operations

Arguments and results are CBOR encodings of the same data model as JSON (null, bool, int, float, text, array, map with text keys), except that binary content is carried as CBOR byte strings or DATA frames where this document says so. Timestamps are floats (seconds since the Unix epoch) or integers as in the engine's JSON API.

## 5. Connection establishment and authentication

```
 Client                                          Server
   | ---- TCP + TLS 1.3 (ALPN "snp/1") ------------ |
   | HELLO {versions, client, features, max_frame} →|
   |            ← HELLO_OK {version, nonce, limits…}|
   | AUTH {method, cnonce, proof} →                 |
   |            ← AUTH_OK {proof, session, …}       |
   | OPEN / MSG / DATA / END … (multiplexed)        |
```

The server MUST arm a handshake deadline of 10 s from TCP accept; if `AUTH_OK` has not been sent by then it MUST close the connection. Before `AUTH_OK` the server MUST accept only HELLO then AUTH, in that order; any other frame is a `protocol_error`.

### 5.1 Server identity

The server holds a long-term key pair and certificate. On first start the engine generates an ECDSA P-256 key and a self-signed certificate (validity ≤ 10 years, SAN = configured names/addresses) under `<data_dir>/snp/` with mode 0600/0700. Its **pin** is `sha256:` followed by the lowercase hex of the SHA-256 of the DER-encoded SubjectPublicKeyInfo (`spki_sha256`) of the leaf certificate. The pin is stable across certificate renewals that keep the key.

A client MUST verify the server by exactly one of:

1. **Pin**: the SHA-256 SPKI of the leaf certificate presented in the TLS handshake equals the profile's configured pin (constant-time comparison). Name and validity-period checks are not performed for pinned identities; the signature in `CertificateVerify` MUST still be verified by TLS.
2. **System trust store** with hostname verification, for deployments using a certificate from a public or private CA.

There is no "insecure" mode. A client that has neither a pin nor a CA-verified chain MUST NOT proceed. A client MAY offer *trust on first use*: it connects once solely to read the presented pin, shows it to the user, and stores it only after explicit confirmation; that probe connection MUST NOT send `AUTH`. A changed pin MUST fail the connection with a distinct error and MUST NOT be auto-accepted.

### 5.2 HELLO / HELLO_OK

`HELLO`: `{ "versions": [u8…] (REQUIRED, non-empty, ≤ 8 entries), "client": text (≤ 64), "features": [text…] (≤ 32), "max_frame": u32 }`

The server selects the highest version it supports from `versions`. If none: send `GOAWAY{code:"unsupported_version", supported:[…]}` and close.

`HELLO_OK`: `{ "version": u8, "server": text, "nonce": bytes(32), "features": [text…], "auth": ["hmac-sha256"], "limits": Limits }`

`Limits` (all REQUIRED, values chosen by the server, the client MUST honour them):

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `max_frame` | 1 048 576 | 16 384 … 16 777 216 | Max payload per frame in either direction after AUTH_OK. MUST be ≤ the HELLO `max_frame` if that was lower. |
| `initial_window` | 262 144 | 16 384 … 2^31−1 | Initial per-stream flow-control credit (octets), each direction. |
| `conn_window` | 4 194 304 | `initial_window` … 2^31−1 | Initial connection-level credit, each direction. |
| `max_streams` | 64 | 1 … 1024 | Concurrent open streams initiated by the client. |
| `ping_interval_ms` | 15 000 | 1 000 … 120 000 | §9. |
| `idle_timeout_ms` | 45 000 | ≥ 2 × `ping_interval_ms` | §9. |
| `idem_ttl_s` | 600 | 0 … 86 400 | Lifetime of idempotency records (§10). |

Defined feature tokens: `events`, `files`, `idempotency`, `reasoning`, `websearch`. A feature is active iff offered by both sides. Unknown tokens are ignored.

### 5.3 AUTH / AUTH_OK

Let `K` be the 32 octets `SHA-256(UTF-8(api_token))`. (The engine stores only this value as `token_sha256`; the raw API token is never stored by the engine and never transmitted by SNP.) Let `spki` be the 32-octet SPKI SHA-256 of the server certificate as defined in §5.1 — computed by the client from the certificate it actually received and by the server from its own certificate.

`AUTH`: `{ "method": "hmac-sha256", "cnonce": bytes(16), "proof": bytes(32) }` with
`proof = HMAC-SHA256(K, "SNP/1 client" ‖ nonce ‖ cnonce ‖ spki)`.

`AUTH_OK`: `{ "proof": bytes(32), "session": text, "api_version": text }` with
`proof = HMAC-SHA256(K, "SNP/1 server" ‖ cnonce ‖ nonce ‖ spki)`.
(`‖` is concatenation; the label strings are ASCII without terminator.)

The server MUST verify the client proof in constant time. The client MUST verify the server proof in constant time and MUST close the connection if it is wrong; this authenticates the server to the client even under trust-on-first-use and binds both proofs to the certificate actually in use, preventing relay to another server. `nonce` MUST be fresh random per connection; `cnonce` MUST be fresh random per connection.

On failure the server MUST send `GOAWAY{code:"unauthenticated"}` and close, after a delay of at least 250 ms × (consecutive failures from that source address, capped at 8), and MUST apply a lock-out of at most 20 failed attempts per source address per rolling minute (further attempts: `GOAWAY{code:"rate_limited"}` without evaluating the proof). Failure reasons MUST NOT distinguish "unknown method" from "bad proof" beyond `unauthenticated`. A client MUST NOT retry authentication automatically after `unauthenticated`, and MUST NOT fall back to any other transport.

Security note: because the engine stores `token_sha256`, an attacker who can read `config.json` can authenticate over SNP (as it could already forge file signatures with `signing_key`). It cannot recover the raw token. The file is mode 0600. Deployments that cannot accept this should restrict network exposure of the SNP port.

## 6. Streams and operations

### 6.1 Lifecycle

A client opens a stream by sending `OPEN` on a new odd stream id.

`OPEN`: `{ "op": text (REQUIRED), "args": map (default {}), "idem": text (OPTIONAL, 1…64 chars), "deadline_ms": u32 (OPTIONAL) }`

Stream states, per endpoint: *idle → open → half-closed(local) / half-closed(remote) → closed*. A sender half-closes by sending `END`. A stream is closed when both directions have sent `END`, or when `ERROR` or `CANCEL` is sent/received. After a stream is closed, late `MSG`, `DATA` and `WINDOW` frames for a stream id the endpoint has previously used MUST be discarded silently (they race with CANCEL); frames for a never-used id above the last opened id are a `protocol_error` (server side) or ignored (client side).

Operation modes (column `mode` in `snp/ops.json`):

| Mode | Client → Server | Server → Client |
|---|---|---|
| `unary` | `OPEN`, then `END` MAY be omitted | `END{result}` or `ERROR` |
| `stream` | `OPEN` | `MSG`*, then `END{result}` or `ERROR` |
| `download` | `OPEN` | `MSG{meta}`, `DATA`*, `END{result}` or `ERROR` |
| `upload` | `OPEN`, `DATA`*, `END` | `END{result}` or `ERROR` |
| `subscribe` | `OPEN`, then `MSG`* (control) | `MSG`* (events) until `END`/`ERROR` |

`END` from the server carries `{ "result": any }` (absent ≡ `null`).

### 6.2 Flow control and backpressure

Each direction of each stream, and each direction of the connection, has a credit counter initialised from `initial_window` / `conn_window`. Sending a `MSG` or `DATA` frame consumes its payload length from both the stream counter and the connection counter; a sender MUST NOT let either go negative and MUST wait for credit. The receiver replenishes credit with `WINDOW{ "inc": u32 }` on the stream (or on stream 0 for the connection). Credit above 2^31−1 is a `protocol_error`. `OPEN`, `END`, `ERROR`, `CANCEL`, `WINDOW`, `PING`, `PONG`, `GOAWAY` frames are not subject to flow control (and are bounded by `max_frame`).

A receiver SHOULD replenish after it has consumed (processed) the data, not on receipt, so that a slow consumer pushes back on the sender and, through the server, on the model or file reader. A server that has been unable to send on a stream for 30 s for lack of credit MUST terminate that stream with `ERROR{code:"stalled"}` and release its resources. For `subscribe` streams a server MAY instead drop non-terminal telemetry events (`system.stats`, `job.progress`, `job.preview`, `download.progress`) while the stream has no credit, and MUST then report the number dropped in the next delivered event under `"dropped"`.

Receivers MUST bound memory per connection by the windows; data exceeding granted credit is a `protocol_error`.

### 6.3 Errors

`ERROR`: `{ "code": text, "status": u16, "message": text (≤ 1024), "retryable": bool, "detail": any (OPTIONAL) }`

`status` is an informational, HTTP-compatible category (400, 401, 403, 404, 409, 413, 429, 499, 500, 503, 504) for use by adapters; it does not imply HTTP transport.

| code | status | retryable | Meaning |
|---|---|---|---|
| `bad_request` | 400 | no | Invalid args / CBOR / semantic validation (handler `HTTPException(400/422)`, `ChatError`, `JobError` etc. carry the handler's own status). |
| `unauthenticated` | 401 | no | Authentication failed (GOAWAY only). |
| `not_found` | 404 | no | Entity missing. |
| `conflict` | 409 | no | State conflict, or idempotency-key reuse with a different operation. |
| `unknown_op` | 404 | no | `op` not in the catalogue. |
| `too_large` | 413 | no | Upload/body exceeds a limit. |
| `rate_limited` | 429 | yes | |
| `resource_exhausted` | 503 | yes | Stream or memory limit; GPU busy where the handler reports so. |
| `cancelled` | 499 | no | Terminated by CANCEL or by connection loss. |
| `deadline_exceeded` | 504 | yes | `deadline_ms` elapsed. |
| `stalled` | 504 | yes | §6.2. |
| `unavailable` | 503 | yes | Upstream (model server, gateway) unreachable. |
| `internal` | 500 | no | Unexpected server fault; details are logged server-side only. |

A handler error whose HTTP-equivalent status is not listed maps by status to the nearest listed code and keeps its numeric `status`. Stream-level errors never close the connection. `ERROR` on stream 0 is not used; connection failures use `GOAWAY`.

### 6.4 Cancellation

`CANCEL{}` on an open stream requests termination. The server MUST stop the underlying work as soon as practicable (e.g. stop token generation, abort the file read, remove the subscription), release all resources of the stream, and reply with `ERROR{code:"cancelled"}` unless the stream already ended. Loss of the connection implies `CANCEL` of every stream on it. A server MUST NOT continue work whose only consumer was cancelled, with the following exception inherited from the engine's chat semantics: a chat generation that is cancelled is persisted with status `stopped` and its partial content retained.

### 6.5 Deadlines

If `deadline_ms` is present the server MUST terminate the operation with `deadline_exceeded` when that many milliseconds have elapsed since it received `OPEN`. A server MAY apply a lower default for `unary` operations (60 s) except for operations marked long-running in the catalogue; it MUST NOT apply a deadline to `stream`, `subscribe`, `upload` and `download` modes beyond stall detection.

## 7. Operations

The operation catalogue is `engine/sarness_engine/snp/ops.json`, schema version `SNP/1`. It is normative for operation names, modes and argument names; the table in §12 is generated from it. Arguments are given in `args` by parameter name: path and query parameters of the equivalent HTTP route as top-level keys, the JSON request body as the map under key `body` (the only exception is `settings.patch`, whose body map is carried under `patch`, as listed in the catalogue). Result values and error conditions are those of the corresponding engine handler. A server MUST reject unknown `op` with `unknown_op`, missing required parameters with `bad_request`, and MUST NOT interpret `args` keys it does not know.

### 7.1 Streaming operations

`chat.generate` — args `{cid, body}`. Server emits `MSG` events, each a map with `type`:

| type | Fields | Notes |
|---|---|---|
| `user_message` | `message` | persisted user turn |
| `title` | `title` | |
| `assistant_start` | `message` | persisted assistant shell, status `partial` |
| `status` | `text` | e.g. model loading |
| `delta` | `content` text, `reasoning` text | Either may be empty; **`reasoning` carries only model-emitted reasoning** (provider `reasoning_content` / `reasoning` fields, or text the engine extracts from the model's own `<think>…</think>` delimiters); it is never synthesised. |
| `search` | `phase` (`started`/`results`/`failed`/`unavailable`), `query`, `sources` | Present only when web search is enabled for the request. |
| `done` | `message` | final persisted assistant message; terminates the stream (followed by `END`) |
| `error` | `message` | generation failed after start; followed by `END` |

A pre-start validation failure is an `ERROR` frame, with no `MSG`.

`agent.completion` — args `{body}`; events `delta` (`content`, `reasoning`, `tool_calls` fragments) and `done` as in the engine's agent completion stream.

`event.subscribe` — `OPEN args {topics: [text…] (default: all)}`. The server sends `MSG{type:"hello", data:{api_version}}` then one `MSG` per engine event, shaped `{type, ts, data}`. The client MAY send `MSG{subscribe:[text…], unsubscribe:[text…]}` at any time (bidirectional control); a topic is the event `type` or a prefix ending in `.` (e.g. `job.`). The queue per subscription is bounded (512 events); see §6.2 for overflow.

### 7.2 Binary operations

`file.get` — args `{fid, range?: {start: u64, end?: u64}}`. Server sends `MSG{meta:{mime, size, start, end, name?}}` then `DATA` frames of at most `max_frame` octets in order, then `END{}`. `end` is inclusive. Content is immutable by id; the client MAY cache by id.

`file.upload` — `OPEN args {name?, mime?, size?}`, then `DATA`* and `END` from the client. `size`, when given, is verified on completion (`bad_request` on mismatch). The server MUST enforce the engine's upload size limit incrementally (counting received octets, not trusting `size`) and respond `too_large` as soon as it is exceeded, discarding the partial data. An upload interrupted by `CANCEL` or connection loss is discarded entirely; no partial object becomes visible. Result: the engine's upload record.

### 7.3 Other operations

All other catalogue operations are `unary`: `OPEN`, `END{result}`.

## 8. Idempotency (OPEN.idem)

If `idempotency` is active, the client SHOULD attach a fresh random `idem` (≥ 128 bits, e.g. UUIDv4) to every non-`GET` unary operation, and reuse the same value when it retries that logical operation after a connection loss or timeout. The server keeps, per authenticated principal, records `(idem → op, outcome)` for `idem_ttl_s`, at most 4096 records (least recently used evicted). On `OPEN` with a known key:

* same `op` and in-flight → the new stream attaches to the in-flight execution and receives its outcome;
* same `op` and finished → the recorded outcome (`END` or `ERROR`) is replayed without re-executing;
* different `op` → `ERROR{conflict}`.

`chat.generate` with a reused `idem` is rejected with `conflict` (a retry must reconcile via `chat.conversation.get`). Records of operations that failed with `retryable:true` are not stored.

## 9. Liveness

If a peer has received no frame for `ping_interval_ms` it MUST send `PING` (8 random octets); the other peer MUST answer with `PONG` carrying the same octets promptly (not subject to credit). A peer that has received no frame at all for `idle_timeout_ms` MUST consider the connection dead, close the TCP connection and treat all streams as `cancelled`. A `PONG` with unexpected data is ignored. An unsolicited `PONG` is ignored.

`GOAWAY`: `{ "code": text, "message": text (≤ 256), "last_stream_id": u32 (OPTIONAL), "supported": [u8…] (OPTIONAL) }`. After sending or receiving GOAWAY the endpoint finishes no new streams and closes the connection; streams with id > `last_stream_id` were not processed and are retryable. Defined codes: `normal`, `protocol_error`, `frame_too_large`, `unsupported_version`, `unauthenticated`, `rate_limited`, `resource_exhausted`, `shutdown`, `idle_timeout`. A server shutting down SHOULD send `GOAWAY{shutdown}` and drain for up to 5 s.

## 10. Reconnection

Streams do not survive a connection. A client SHOULD reconnect with exponential back-off (initial 500 ms, factor 2, cap 30 s, ±25 % jitter) after transport failure, `idle_timeout`, `shutdown` and `resource_exhausted`; it MUST NOT auto-reconnect after `unauthenticated`, `unsupported_version`, pin mismatch or a failed server proof. After reconnecting, `event.subscribe` streams are re-established by the client; operations are retried only if idempotent (GET-equivalent) or carry `idem`.

## 11. Resource limits and malformed input

The following are RECOMMENDED server defaults; every value MUST be configurable and every limit MUST be enforced, with the stated outcome:

| Resource | Limit | Outcome |
|---|---|---|
| Concurrent connections | 32 total, 8 per source address | excess TCP connections closed before TLS completes |
| Pre-auth frame size | 4096 octets | `GOAWAY(frame_too_large)` |
| Handshake time | 10 s | close |
| Streams per connection | `max_streams` | `ERROR{resource_exhausted}` on the offending OPEN |
| Frame size | `max_frame` | `GOAWAY(frame_too_large)` |
| Per-connection buffered inbound data | ≤ `conn_window` | `GOAWAY(protocol_error)` on overrun |
| Subscription queue | 512 events | §6.2 |
| `args` / `body` size | 4 MiB per OPEN+MSG on a unary operation | `ERROR{too_large}` |
| Idempotency table | 4096 records | LRU |

Any violation of §4 (magic, flags, type, length, stream id parity/order, window overflow, malformed CBOR in a handshake frame, frames out of handshake order) is a `protocol_error`: the endpoint SHOULD send `GOAWAY{protocol_error}` and MUST close the TCP connection. Malformed CBOR inside a stream-level payload terminates only that stream (`bad_request`). A peer that sends data after `GOAWAY` has been sent is disconnected without further reply. Implementations MUST NOT log secrets, proofs, nonces or payload bodies at default log levels.

## 12. Conformance

An implementation conforms to SNP/1 if it satisfies every MUST in this document. The reference test-suite (`engine/tests/test_snp_*.py`, `desktop/src-tauri/src/snp/tests.rs`) exercises: framing and CBOR-profile rejection vectors, version negotiation, handshake and authentication success and failure, pin mismatch, window enforcement and stall termination, cancellation, deadlines, idempotent replay, malformed/oversized frames, connection-limit exhaustion, interrupted uploads and downloads, concurrent sessions and reconnection.

### 12.1 Operation catalogue

Generated from `snp/ops.json`: see `docs/SNP-OPS.md`.

### 12.2 Test vectors

Auth: `K = SHA-256("sk-test")`; with `nonce = 00…00` (32 octets), `cnonce = 11…11` (16 octets), `spki = 22…22` (32 octets), `proof = HMAC-SHA256(K, "SNP/1 client" ‖ nonce ‖ cnonce ‖ spki)`. Implementations MUST include this vector in their tests Expected `K = f3abf2a6cc4f00987743db5f544ba345b4899ae31f326d8ee9c4816de153c9e0` and expected client `proof = 5b2fc2282c3e222fcbeb00e7b86d643140edad01fbe84ed8c8b473418c797b3f` (computed with OpenSSL, independent of both implementations).

Framing: the frame `53 4E 05 00 00 00 00 00 00 00 00 08 01 02 03 04 05 06 07 08` is a valid PING on stream 0.

## 13. Clarifications (normative)

These points were unspecified in earlier drafts of this document and were fixed by the two reference implementations; independent implementations MUST behave identically.

1. **No fragmentation.** A `MSG` payload MUST NOT exceed the stream's available credit window or `max_frame`; `END` and `ERROR` payloads MUST NOT exceed `max_frame`. A server whose unary result would exceed `max_frame` MUST answer `ERROR{too_large, status 413}`; any other oversized `MSG` is likewise `too_large`. For `chat.generate`, a final `done` event whose message does not fit is sent as `{type:"done", message:{…, content:"", reasoning:"", truncated:true}}`; the client MUST then read the persisted message with `chat.conversation.get`. `OPEN` is itself bounded by `max_frame`, so `args` larger than `max_frame` cannot be sent; the 4 MiB limit in §11 is an upper bound on what a server accepts, not a transfer mechanism.
2. **Subscription topics.** On `event.subscribe`, an absent `topics` key means all events; `topics: []` means none. In all-events mode a client `MSG{unsubscribe:[t]}` adds an exclusion and `MSG{subscribe:[t]}` removes one. The server's first `MSG` is `{type:"hello", data:{api_version}}`. SNP defines no heartbeat event; liveness is PING/PONG (§9). Adapters that must present a WebSocket-style heartbeat synthesise it locally.
3. **`file.get`.** `MSG{meta}` contains `mime, size` (total file size), `start`, `end` (inclusive end of the served range), `length` (octets that follow) and optionally `name`. For an empty file: `size 0, start 0, end 0, length 0`.
4. **Implicit close.** A server `END` or `ERROR` closes the stream completely; the client need not half-close first.
5. **Long-running operations.** Operations whose names begin with `llm.`, `paintbrush.`, `runtime.`, `model.`, `worker.`, `hf.`, `gateway.` or `experiment.`, plus `generation.bulk` and `chat.conversation.import`, are long-running: servers apply no default deadline, and clients SHOULD send an explicit `deadline_ms`. Every other `unary` operation has a server default deadline of 60 s.
6. **Misdirected frames.** A client `MSG` on a stream that is not a `subscribe` stream, or `DATA` on a stream that is not an `upload`, is a stream-level `bad_request`.
7. **Features.** `HELLO_OK.features` is the intersection of the features offered by both sides.
8. **Authentication of all operations.** Every operation, including `system.health`, requires a completed `AUTH`.
9. **Zero window increments.** `WINDOW{inc:0}` MUST be ignored.
10. **After GOAWAY.** A server that has sent `GOAWAY{shutdown}` disconnects without reply on any further `OPEN`. A `GOAWAY` without `last_stream_id` carries no claim about which streams were processed: the receiver MUST NOT assume any stream was unprocessed (and therefore MUST NOT retry non-idempotent operations without an idempotency key).
11. **Slot accounting.** A client MAY release a stream slot at the moment it sends `CANCEL`; a server MAY keep counting the slot until it has sent the stream's terminal frame, so a client that reopens immediately can receive `resource_exhausted` (retryable).
12. **Text results.** `chat.conversation.export` with `format=md` returns a CBOR text string as `result`; with `format=json` a map.
13. **Argument naming.** Arguments are named after the handler parameters: the request body is under `body`, except for `settings.patch`, whose body is under `patch`.
14. **Argument typing.** Catalogue entries carry no parameter types. Query-style parameters defined by the engine as integers or booleans (`limit`, `favorite`, `trash`, `permanent`, `presets`, `since`, `until`, `before`) MUST be sent as CBOR integers, booleans or floats, not as text; an adapter converting from text query strings coerces by name.
