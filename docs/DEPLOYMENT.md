# Sarness deployment reference

Applies to Sarness Engine 0.4.x and Sarness Desktop 0.2.x. The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. This document is a reference, not a tutorial: it assumes working knowledge of Linux service management, TLS, SSH port forwarding, NVIDIA driver/CUDA packaging and stateful firewalls. Wire-level behaviour of the native protocol is specified in [SNP.md](SNP.md); the HTTP interface in [API.md](API.md).

## 1. Components and trust boundaries

| Component | Runs on | Privilege | Persistent state |
|---|---|---|---|
| Sarness Engine | GPU host | unprivileged user, systemd *user* unit | `<data_dir>` (default `~/.local/share/sarness-engine`): `config.json` (0600), `sarness.db` (SQLite, WAL), `models/`, `outputs/`, `uploads/`, `thumbs/`, `bin/`, `logs/`, `snp/` (0700) |
| Sarness Desktop | operator workstation | unprivileged user | `~/.config/sarness/profiles.json` (non-secret), API token in the Secret Service keyring (fallback `~/.config/sarness/secrets.json`, 0600), media cache under the XDG cache dir |

The webview never holds the API token and never connects to the engine. It talks only to a per-launch, session-keyed loopback interface served by the desktop's native process; that process alone speaks to the engine over the selected transport (§6).

## 2. Requirements

| Item | Requirement |
|---|---|
| OS | Linux x86-64 with systemd; Ubuntu 22.04 or newer (26.04 exercised) |
| GPU | NVIDIA, driver ≥ 570 for the CUDA 12.8 wheel set; ≥ 560 selects CUDA 12.6 wheels. The engine never installs or modifies drivers. |
| Python | ≥ 3.11 with `venv` |
| Python packages | `fastapi`, `uvicorn[standard]`, `httpx`, `psutil`, `nvidia-ml-py`, `pillow`, `numpy`, `huggingface_hub`, `python-multipart`, **`cryptography ≥ 42`** (SNP identity; required from 0.4.0). Inference extra: `torch`, `torchvision`, `diffusers`, `transformers`, `accelerate`, … |
| Disk | ≈ 15 GB runtime plus model storage |
| RAM | ≥ 24 GB for Wan video on a 12 GB GPU (the UMT5 encoder is held in RAM) |
| Reachability | The desktop MUST be able to open a TCP connection to the engine's SNP or HTTP port directly, or through an SSH local forward. |

## 3. Network endpoints and firewall policy

| Port (default) | Protocol | Config key | Authentication | Exposure |
|---|---|---|---|---|
| 8742/tcp | HTTP/JSON ([API.md](API.md)) | `port` | `Authorization: Bearer` | `host` |
| 8744/tcp | SNP/1 over TLS 1.3, ALPN `snp/1` | `snp_port` | HMAC challenge–response (SNP.md §5.3) | `snp_host`, else `host` |

The two listeners are independent. The engine MUST keep serving HTTP when SNP fails to bind; a bind failure is logged at error level as `SNP/1 FAILED TO START`. Neither listener binds to `0.0.0.0` unless `host`/`snp_host` is set to it explicitly.

The engine does not manage packet filters. A host with a default-deny inbound policy MUST be given an explicit rule for 8744/tcp, scoped to the narrowest source range that contains the clients, e.g.

```
ufw allow from 10.0.0.0/24 to any port 8744 proto tcp comment 'Sarness SNP (LAN only)'
```

Neither port is intended for exposure to untrusted networks. SNP resists unauthenticated resource exhaustion by construction (§11 of SNP.md: pre-authentication frames ≤ 4096 octets, 10 s handshake deadline, per-address connection and failure limits) but it is not a substitute for network-level access control.

## 4. Installation

### 4.1 Guided (desktop → SSH)

Preconditions: key-based, non-interactive `ssh user@host`. The desktop neither requests nor stores SSH passwords. The installer copies the bundled engine, installs into `~/.local/share/sarness-engine-app` (venv), creates `sarness-engine.service` as a user unit, starts it, waits for `/v1/health`, issues an API token and stores it in the desktop keyring. It does not use `sudo`, alter firewall rules, install drivers, touch unrelated services or delete outside its own directories. Re-running upgrades in place, preserves models, generations and settings, and issues a new token to the invoking desktop.

Listener binding modes: *private-network* (interface address of the private network; loopback if none), *loopback* (to be used with an SSH forward) and *LAN address* (primary IPv4 address; plaintext HTTP, trusted networks only). The installer-created profile uses the HTTP transport; switch the profile to SNP afterwards (§6).

### 4.2 Manual

```
bash install-engine.sh --source sarness-engine.tar.gz [--with-llama] [--bind tailscale|loopback|lan] [--port 8742] [--keep-token] [--no-service]
```

The final output line is a JSON object containing the API token, printed once. `--bind tailscale` selects the private-network interface address (name retained for compatibility). Without the helper script:

```
python3 -m venv ~/sarness-venv
~/sarness-venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
~/sarness-venv/bin/pip install "./engine[inference]"
~/sarness-venv/bin/sarness-engine init --host <address>      # prints the token
~/sarness-venv/bin/sarness-engine serve
```

An in-place upgrade from < 0.4.0 that uses `pip install --no-deps` MUST additionally install `cryptography>=42` into the same environment; without it the SNP service cannot create its identity and does not start (HTTP continues).

### 4.3 Service management

```
systemctl --user {status|restart|stop} sarness-engine
journalctl --user -u sarness-engine -f          # also <data_dir>/logs/engine.log
loginctl enable-linger "$USER"                  # run without an active session
sarness-engine doctor
sarness-engine token --rotate                   # invalidates the current token for HTTP and SNP
sarness-engine snp-identity                     # pin, certificate path, listen address; never key material
```

## 5. Engine configuration

`<data_dir>/config.json` (mode 0600). Unknown keys are ignored and absent keys take the defaults shown; files written by earlier versions load unchanged.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | HTTP bind address |
| `port` | `8742` | HTTP port |
| `token_sha256` | — | Lowercase hex SHA-256 of the API token. Verifier for HTTP bearer comparison and key material for the SNP proof (SNP.md §5.3). |
| `signing_key` | random | HMAC key for signed file URLs |
| `cors_origins` | `[]` | Additional CORS origins (HTTP only) |
| `snp_enabled` | `true` | Start the SNP listener |
| `snp_host` | `null` (= `host`) | SNP bind address |
| `snp_port` | `8744` | SNP port |
| `snp_names` | `[]` | Extra DNS names/IPs placed in the certificate's subjectAltName |

Environment overrides: `SARNESS_HOME`, `SARNESS_HOST`, `SARNESS_PORT`, `SARNESS_SNP_HOST`, `SARNESS_SNP_PORT`. Runtime settings (SQLite `settings` table, `GET/PATCH /v1/settings`) are separate and are listed in API.md. SNP protocol limits (SNP.md §11) are the specified defaults; they are parameters of the in-process server and are not yet exposed in `config.json`.

## 6. Desktop connection profiles

Transport (what is spoken) and tunnelling (how bytes reach the host) are orthogonal.

| Transport | Remote port | Server authentication | Client authentication |
|---|---|---|---|
| `snp` | `snp_port` | TLS 1.3 + pinned SPKI (or CA chain + name) + server proof | HMAC proof over server nonce, client nonce and SPKI |
| `https` | `port` | System trust store | Bearer |
| `http` | `port` | none | Bearer (cleartext) |

`mode = ssh` forwards the remote port of the active transport (`ssh -N -L 127.0.0.1:<free>:<host>:<port>`, supervised and restarted with back-off). TLS and authentication remain end-to-end through the forward; with SNP the pin is the engine's, not the tunnel's. `profile.host` is then the engine address *as seen from the SSH server*.

Profile fields (`profiles.json`): `id, name, host, port, scheme, mode, ssh{user,host,port,key_path}`, plus since 0.2.0 `transport` (`snp|http|https`), `snp_port`, `snp_pin`, `snp_server_name`. A profile lacking `transport` takes it from `scheme`; no stored profile or token is rewritten or invalidated by the upgrade.

### 6.1 Server identity (SNP)

The engine creates `<data_dir>/snp/server.key` (ECDSA P-256, 0600) and `server.crt` on first start (self-signed; subjectAltName = `localhost`, `127.0.0.1`, `::1`, the bind host unless it is a wildcard, and `snp_names`). The *pin* is `sha256:` ‖ hex(SHA-256(DER SubjectPublicKeyInfo of the leaf)). It depends only on the key, so certificate regeneration with the same key preserves it. Independent verification:

```
echo | openssl s_client -connect HOST:8744 -alpn snp/1 -tls1_3 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum
```

Rotation: stop the engine, delete `server.key` and `server.crt`, start it, and re-pin every client. A client MUST treat the new pin as an identity change (§7).

Trust on first use is supported only as an explicit operator action: *Fetch identity* opens one connection that sends no credentials, displays the presented pin, and stores it only after confirmation. The client verifies CA-issued certificates when the pin is left empty and `snp_server_name` is supplied (chain and name against the system trust store). **The engine, however, only serves the certificate it generates itself** in this release: it regenerates `server.crt` whenever its SAN set differs from the expected one and has no setting for an operator-supplied certificate. Serving a CA certificate is therefore not yet possible; use pinning.

## 7. Failure states (SNP)

| Condition | Client classification | Auto-reconnect | Operator action |
|---|---|---|---|
| DNS failure / connection refused / handshake timeout | `Dns`, `Refused`, `Timeout` | yes (500 ms → 30 s, ±25 % jitter) | check address, listener, firewall |
| Peer closes during TLS / not TLS | `Tls`, `Io` | yes | verify port is the SNP port, not 8742 |
| TLS version/ALPN rejected | `Tls`, `Alpn` | no | not an SNP endpoint |
| SPKI ≠ configured pin | `PinMismatch{expected, presented}` | **no** | verify out of band; re-fetch identity only if the change is expected |
| Self-signed chain with no pin | `UntrustedCertificate` | no | pin the identity or use a CA certificate |
| No common version | `UnsupportedVersion{supported}` | no | upgrade the older side |
| Bad token (`unauthenticated`) | `Unauthenticated` | **no** | correct the token; retrying is a lock-out risk |
| 20 failures/minute from one source | `RateLimited` (wrong proof only) | after back-off | wait; investigate the source; a correct proof is still accepted |
| Server proof invalid | `ServerProof` | **no** | the peer does not hold the token verifier: treat as an impersonation attempt |
| Malformed or oversized frame, window overflow | `Protocol`, `Limit` | after back-off | report; indicates a defective peer |
| Idle timeout, reset, engine restart | `ConnectionLost` | yes | none (streams in flight are cancelled; chat replies are kept as `stopped`) |

SNP never falls back to HTTP after any of the above, and HTTP never falls back to SNP.

## 8. Authentication model

The engine stores only `token_sha256`. HTTP compares `SHA-256(presented token)` against it. SNP uses the same 32 octets as the HMAC key; the raw token never crosses the network in either transport (SNP) and is never stored by the engine. A reader of `config.json` (or of a backup containing it) can therefore authenticate over SNP, with the same reach as the token itself (file transfer, model installation, chat, jobs), and can forge file signatures with `signing_key`; it cannot recover the token or use it on another service. Protect `config.json` and engine backups accordingly. `config.json` MUST remain mode 0600. Rotating the token invalidates both transports.

HTTP additionally rate-limits failures (20 per minute per address, status 429) and never logs query strings. The engine never enables `trust_remote_code`; uploads are content-sniffed and size-limited; paths resolve only inside `<data_dir>`.

## 9. Chat reasoning and web search (operational notes)

*Reasoning.* The engine forwards provider-exposed reasoning (`reasoning_content`, Ollama `reasoning`) verbatim in the `reasoning` field of `delta` events and persists it per message. If a provider leaves a leading `<think>…</think>` block in `content`, that single block is moved to `reasoning`; no other text is ever reclassified. Models that expose no reasoning produce an empty `reasoning` field. Reasoning is stored with, and only with, its message; it is not re-sent to the model on later turns.

*Web search* (Chat only; never offered to the local coding agent or generation modes). Settings: `search_provider` (`auto|searxng|duckduckgo|brave`), `search_searxng_url`, `search_brave_key` (write-only), `search_policy` (`manual|always`), `search_max_results`, `search_fetch_pages`, `search_timeout_s`. `auto` selects SearXNG when its URL is set, else DuckDuckGo's keyless endpoint. The engine performs the query and fetches pages itself; page fetches resolve the host, refuse non-global addresses, connect to the vetted IP with the original Host/SNI, re-vet every redirect (≤ 3), accept only `text/html`, `application/xhtml+xml`, `text/plain` and cap bodies at 1 MB. Results enter the prompt as delimited, explicitly untrusted data; `</source` sequences in page text are neutralised. A failed search is reported to the client (`search` event, phase `unavailable`) and to the model as such; the answer is never presented as search-backed. SearXNG instances MUST have `json` in `search.formats`.

## 10. Upgrade, backup, rollback

1. Preflight: no running or queued jobs in the engine or any gateway it fronts, no active chat streams, idle GPU.
2. Back up: `config.json`; `sarness.db` via the SQLite online-backup API (not `cp` of a live WAL database); the installed package and dist-info; the unit file; the engine source copy. Store under `~/sarness-backups/engine-<version>-<UTC>/`.
3. Install: unpack the new source, `pip install --no-deps --no-build-isolation <src>` (plus new dependencies, §4.2), restart the unit, poll `/v1/health`, check both listeners with `ss -ltn`.
4. Rollback: stop the unit, `pip install --force-reinstall` the saved source, restore `config.json` and the unit file, start the unit. Restore `sarness.db` only if a schema migration ran (check `PRAGMA user_version` before and after); 0.3.0 → 0.4.0 performs no schema change.

Engine restart cancels in-flight streams; clients reconnect automatically and chat messages cut off by the restart remain as `stopped`.

## 11. Gateways

A server that already runs an HTTP AI gateway owning the GPU (`/models`, `/uploads`, `/jobs/image`, `/jobs/video`, OpenAI-compatible `/v1`) is integrated through `gateway_url`/`gateway_token` (engine settings) and, for chat, a remote endpoint. Image and video models appear as `gw:<id>`; jobs run through the Sarness queue while inference runs in the gateway. The gateway token is stored as an engine secret and never returned. Gateway models support the standard simple-mode graph only. Before a gateway job the engine releases GPU memory it holds itself. VRAM is shared with whatever else the gateway schedules: loading an additional large model while a video job occupies the card can terminate the job (observed with a 12 GB device; run the two sequentially).

## 12. Compatibility matrix

| Desktop | Engine | HTTP API | SNP | Result |
|---|---|---|---|---|
| 0.1.x | 0.4.x | 1.1 | — | HTTP/HTTPS works unchanged |
| 0.2.x | 0.3.x | 1.1 | not offered | HTTP/HTTPS only; SNP profile fails with `Refused` |
| 0.2.x | 0.4.x | 1.1 | 1 | all transports |
| any | any | major ≠ | — | client MUST refuse |

SNP version negotiation (`HELLO.versions`) is independent of the HTTP `api_version`; the two evolve separately.

## 13. Private-network diagnostics

Server discovery (*Find servers → Scan*) reads the devices the system already knows and probes only the engine health endpoint; it never scans ports. When Tailscale is present on the desktop its online peers (`tailscale status --json`) are included; MagicDNS names and `100.64.0.0/10` addresses are ordinary host values everywhere. If a peer is missing: confirm `tailscale status` on both hosts, use the peer's MagicDNS name or address directly, and re-fetch the SNP identity if the server was reinstalled.
