// Transport to the engine.
//
// In the desktop app all traffic goes through the Rust loopback proxy, which
// injects the API token; the webview only holds a per-launch session key.
// In a plain browser (UI development), VITE_ENGINE_URL / VITE_ENGINE_TOKEN
// are used directly — never shipped in production builds.

export const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

export interface Transport {
  base: string;
  headers: Record<string, string>;
  /** query string fragment that authenticates media URLs, e.g. "s=…" */
  mediaQuery: string;
  connected: boolean;
  /** The desktop proxy routes `/e/<engine>/v1/...` to one of several connected engines. */
  engines?: boolean;
}

let current: Transport = { base: '', headers: {}, mediaQuery: '', connected: false };
const listeners = new Set<(t: Transport) => void>();

export function transport(): Transport {
  return current;
}

export function setTransport(t: Transport) {
  current = t;
  for (const l of listeners) l(t);
}

export function onTransport(fn: (t: Transport) => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function proxyTransport(url: string, session: string, connected: boolean): Transport {
  return { base: url, headers: { 'x-sarness-session': session }, mediaQuery: `s=${encodeURIComponent(session)}`, connected, engines: true };
}

/**
 * Base URL for one engine. `engine` is the server profile id; without one (or without a
 * multi-engine proxy, as in browser development) requests go to the primary engine.
 */
export function engineBase(engine?: string | null): string {
  return engine && current.engines ? `${current.base}/e/${encodeURIComponent(engine)}` : current.base;
}

export function devTransport(): Transport | null {
  const url = import.meta.env.VITE_ENGINE_URL as string | undefined;
  // Dev only: a headless loopback proxy (any engine transport) with its session key.
  const session = import.meta.env.VITE_ENGINE_SESSION as string | undefined;
  if (url && session && import.meta.env.DEV) return proxyTransport(url.replace(/\/$/, ''), session, true);
  const token = import.meta.env.VITE_ENGINE_TOKEN as string | undefined;
  if (!url || !token || !import.meta.env.DEV) return null;
  return { base: url.replace(/\/$/, ''), headers: { Authorization: `Bearer ${token}` }, mediaQuery: `token=${encodeURIComponent(token)}`, connected: true };
}

/** Absolute URL for an engine-relative media path such as `/v1/files/f_…`, on `engine` (default: primary). */
export function mediaUrl(path: string | null | undefined, engine?: string | null): string {
  if (!path) return '';
  if (path.startsWith('data:') || path.startsWith('blob:')) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${engineBase(engine)}${path}${sep}${current.mediaQuery}`;
}

export function wsUrl(path: string, engine?: string | null): string {
  const base = engineBase(engine).replace(/^http/, 'ws');
  const sep = path.includes('?') ? '&' : '?';
  return `${base}${path}${sep}${current.mediaQuery}`;
}
