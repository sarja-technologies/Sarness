// Connection transport helpers (pure, no Tauri): labels, endpoint text, pin handling.
//
// "Transport" is the application protocol (SNP / HTTP / HTTPS); "mode" is how the
// bytes get there (direct or through an SSH tunnel). The two are independent.

export type Transport = 'snp' | 'http' | 'https';

export const DEFAULT_SNP_PORT = 8744;
export const DEFAULT_HTTP_PORT = 8742;

export const TRANSPORTS: { value: Transport; label: string; title: string; hint: string }[] = [
  { value: 'snp', label: 'SNP', title: 'SNP - native protocol', hint: 'SNP - native protocol: one encrypted, authenticated, multiplexed connection with pinned server identity.' },
  { value: 'http', label: 'HTTP', title: 'HTTP - direct', hint: 'HTTP - direct: the classic JSON API without transport encryption. Use it on a trusted network or through an SSH tunnel.' },
  { value: 'https', label: 'HTTPS', title: 'HTTPS - encrypted HTTP', hint: 'HTTPS - encrypted HTTP: the JSON API behind a TLS certificate your system trusts.' },
];

export function transportLabel(t: Transport | string | null | undefined): string {
  return TRANSPORTS.find((x) => x.value === t)?.label ?? 'HTTP';
}

export function transportHint(t: Transport): string {
  return TRANSPORTS.find((x) => x.value === t)?.hint ?? '';
}

interface EndpointProfile {
  transport?: Transport;
  scheme?: string;
  host: string;
  port: number;
  snp_port?: number;
  mode: 'direct' | 'ssh';
  ssh?: { user: string; host: string } | null;
}

/** The transport of a profile, falling back to its legacy `scheme` for old data. */
export function profileTransport(p: Pick<EndpointProfile, 'transport' | 'scheme'>): Transport {
  return p.transport ?? (p.scheme === 'https' ? 'https' : 'http');
}

/** Port used by the profile's transport. */
export function activePort(p: Pick<EndpointProfile, 'transport' | 'scheme' | 'port' | 'snp_port'>): number {
  return profileTransport(p) === 'snp' ? (p.snp_port ?? DEFAULT_SNP_PORT) : p.port;
}

/** One-line description of where a profile connects, e.g. `snp://host:8744` or `ssh me@box → http://127.0.0.1:8742`. */
export function endpointLabel(p: EndpointProfile): string {
  const t = profileTransport(p);
  const host = p.host.includes(':') && !p.host.startsWith('[') ? `[${p.host}]` : p.host;
  const url = `${t}://${host}:${activePort(p)}`;
  return p.mode === 'ssh' ? `ssh ${p.ssh?.user ?? ''}@${p.ssh?.host ?? ''} → ${url}` : url;
}

const PIN_RE = /^sha256:[0-9a-f]{64}$/;

export function normalizePin(s: string | null | undefined): string {
  return (s ?? '').trim().toLowerCase();
}

export function isValidPin(s: string | null | undefined): boolean {
  return PIN_RE.test(normalizePin(s));
}

/** `sha256:ab12…` → `AB:12:…` for side-by-side comparison with the server. */
export function pinFingerprint(pin: string): string {
  const hex = normalizePin(pin).replace(/^sha256:/, '');
  return (hex.match(/.{1,2}/g) ?? []).join(':').toUpperCase();
}

/** Shortened pin for tight spaces (tooltips, status bar). */
export function shortPin(pin: string | null | undefined): string {
  const p = normalizePin(pin);
  return p.length > 20 ? `${p.slice(0, 15)}…${p.slice(-6)}` : p;
}

export interface SnpNegotiated {
  version: number;
  server: string;
  features: string[];
  api_version: string;
  pin: string;
  handshake_ms: number;
  limits: { max_frame: number; initial_window: number; conn_window: number; max_streams: number };
}

/** Human summary of a negotiated SNP session. */
export function describeSession(s: SnpNegotiated): string {
  const feats = s.features.length ? s.features.join(', ') : 'no optional features';
  return `SNP v${s.version} · ${s.server || 'engine'} · API ${s.api_version || '?'} · ${feats}`;
}

export function describeLimits(l: SnpNegotiated['limits']): string {
  const kib = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(n % 1048576 ? 1 : 0)} MiB` : `${Math.round(n / 1024)} KiB`);
  return `frames up to ${kib(l.max_frame)} · window ${kib(l.initial_window)}/${kib(l.conn_window)} · ${l.max_streams} streams`;
}

/** Form values that are invalid for the chosen transport, as user-facing text (empty = fine). */
export function validateForm(p: { host: string; transport: Transport; port: number; snp_port: number; snp_pin?: string | null }): string {
  if (!p.host.trim()) return 'Enter the server address.';
  const port = p.transport === 'snp' ? p.snp_port : p.port;
  if (!Number.isInteger(Number(port)) || Number(port) < 1 || Number(port) > 65535) return 'The port must be between 1 and 65535.';
  if (p.transport === 'snp' && p.snp_pin && !isValidPin(p.snp_pin)) return 'The server identity must look like sha256: followed by 64 hexadecimal digits.';
  return '';
}
