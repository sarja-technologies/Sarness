// Server connection: profiles, the desktop proxy, live event stream, health.

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { api } from '../api/client';
import { devTransport, isTauri, proxyTransport, setTransport, transport, wsUrl } from '../api/transport';
import type { EngineEvent, LiveStats, SystemInfo } from '../api/types';
import type { SnpNegotiated, Transport } from './transport';

export interface SshOptions {
  user: string;
  host: string;
  port: number;
  key_path?: string | null;
}

export interface Profile {
  id: string;
  name: string;
  host: string;
  port: number;
  /** Legacy mirror of `transport` for the HTTP transports. */
  scheme: 'http' | 'https';
  /** How the bytes get there (independent of the protocol spoken over them). */
  mode: 'direct' | 'ssh';
  ssh?: SshOptions | null;
  has_token: boolean;
  /** Application protocol: SNP (native), HTTP or HTTPS. */
  transport: Transport;
  snp_port: number;
  /** Pinned server identity, `sha256:<64 hex>`. */
  snp_pin?: string | null;
  /** Host name to verify when the server uses a CA-issued certificate (no pin). */
  snp_server_name?: string | null;
}

export type SnpFailure = 'dns' | 'refused' | 'timeout' | 'tls' | 'pin_mismatch' | 'untrusted_certificate' | 'version' | 'auth'
  | 'rate_limited' | 'server_proof' | 'protocol' | 'limit' | 'connection' | 'goaway' | 'ssh';

export interface TestResult {
  ok: boolean;
  stage: string;
  message: string;
  latency_ms: number | null;
  health: Record<string, unknown> | null;
  system: SystemInfo | null;
  transport?: Transport;
  /** Negotiated SNP session (no secrets). */
  snp?: (SnpNegotiated & { fingerprint: string }) | null;
  error_kind?: SnpFailure | null;
  /** Pin saved in the profile / pin the server presented (pin mismatch, untrusted certificate). */
  expected_pin?: string | null;
  presented_pin?: string | null;
}

export interface ProxyInfo {
  url: string;
  session: string;
  connected: boolean;
  profile_id: string | null;
  transport: Transport | null;
  snp: { status: { state: string; error: string | null; error_kind: string | null; fatal: boolean }; session: (SnpNegotiated & { session?: string }) | null } | null;
}

export type ConnState = 'booting' | 'unconfigured' | 'connecting' | 'online' | 'offline';

export const conn = $state({
  state: 'booting' as ConnState,
  profiles: [] as Profile[],
  activeId: null as string | null,
  system: null as SystemInfo | null,
  stats: null as LiveStats | null,
  lastError: null as string | null,
  wsConnected: false,
  tunnel: null as string | null,
  lastEventAt: 0,
  /** Transport of the active connection and, for SNP, the negotiated session. */
  transport: null as Transport | null,
  snp: null as ProxyInfo['snp'],
});

function applyProxyInfo(info: Pick<ProxyInfo, 'transport' | 'snp'>) {
  conn.transport = info.transport ?? null;
  conn.snp = info.snp ?? null;
}

/** Re-reads transport and negotiated SNP details from the desktop core. */
export async function refreshTransportInfo() {
  if (!isTauri) return;
  try {
    applyProxyInfo(await invoke<ProxyInfo>('proxy_info'));
  } catch {
    /* informational only */
  }
}

export const activeProfile = () => conn.profiles.find((p) => p.id === conn.activeId) ?? null;

type Handler = (e: EngineEvent) => void;
const handlers = new Map<string, Set<Handler>>();

/** Subscribe to engine events by type ("job.updated") or prefix ("job.*"). */
export function onEvent(type: string, fn: Handler): () => void {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type)!.add(fn);
  return () => handlers.get(type)?.delete(fn);
}

function dispatch(e: EngineEvent) {
  conn.lastEventAt = Date.now();
  handlers.get(e.type)?.forEach((h) => h(e));
  const prefix = e.type.split('.')[0] + '.*';
  handlers.get(prefix)?.forEach((h) => h(e));
  handlers.get('*')?.forEach((h) => h(e));
}

// ------------------------------------------------------------- websocket
let ws: WebSocket | null = null;
let wsTimer: ReturnType<typeof setTimeout> | null = null;
let watchdog: ReturnType<typeof setInterval> | null = null;
let backoff = 1000;
let wantWs = false;

function openWs() {
  if (!wantWs || !transport().base) return;
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  const sock = new WebSocket(wsUrl('/v1/events'));
  ws = sock;
  sock.onopen = () => {
    trace('event stream connected');
    backoff = 1000;
    conn.wsConnected = true;
    conn.lastEventAt = Date.now();
    if (conn.state === 'offline') {
      conn.state = 'online';
      refreshSystem();
      dispatch({ type: 'connection.restored', data: {} });
    }
  };
  sock.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data);
      if (ev.type === 'system.stats') conn.stats = ev.data;
      dispatch(ev);
    } catch {
      /* ignore malformed */
    }
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    conn.wsConnected = false;
    if (wantWs) {
      if (conn.state === 'online') conn.state = 'offline';
      scheduleWs();
    }
  };
  sock.onerror = () => sock.close();
}

function scheduleWs() {
  if (wsTimer) clearTimeout(wsTimer);
  wsTimer = setTimeout(openWs, backoff);
  backoff = Math.min(backoff * 1.7, 15000);
}

function startWs() {
  wantWs = true;
  openWs();
  if (watchdog) clearInterval(watchdog);
  // Heartbeats arrive every 15s; silence for 40s means a dead connection.
  watchdog = setInterval(() => {
    if (conn.wsConnected && Date.now() - conn.lastEventAt > 40000) {
      conn.wsConnected = false;
      ws?.close();
      conn.state = 'offline';
      scheduleWs();
    }
  }, 5000);
}

function stopWs() {
  wantWs = false;
  if (wsTimer) clearTimeout(wsTimer);
  if (watchdog) clearInterval(watchdog);
  ws?.close();
  ws = null;
  conn.wsConnected = false;
}

// ---------------------------------------------------------------- actions
function trace(msg: string) {
  (window as any).__sarnessLog?.('info', msg);
}

export async function refreshSystem() {
  try {
    conn.system = await api.get<SystemInfo>('/v1/system');
    trace(`system info loaded from ${conn.system.hardware.hostname}`);
    void refreshTransportInfo();
    conn.lastError = null;
    if (conn.state !== 'online') conn.state = 'online';
  } catch (e) {
    conn.lastError = (e as Error).message;
    (window as any).__sarnessLog?.('warn', `system request failed: ${conn.lastError}`);
    if (conn.state === 'online' || conn.state === 'connecting') conn.state = 'offline';
  }
  return conn.system;
}

export async function loadProfiles() {
  if (!isTauri) return;
  const store = await invoke<{ profiles: Profile[]; active: string | null }>('list_profiles');
  conn.profiles = store.profiles;
  conn.activeId = store.active;
}

export async function initConnection() {
  if (!isTauri) {
    const dev = devTransport();
    if (!dev) {
      conn.state = 'unconfigured';
      return;
    }
    setTransport(dev);
    conn.profiles = [{ id: 'dev', name: 'Development engine', host: dev.base, port: 0, scheme: 'http', mode: 'direct', has_token: true, transport: 'http', snp_port: 8744 }];
    conn.transport = 'http';
    conn.activeId = 'dev';
    conn.state = 'connecting';
    await refreshSystem();
    startWs();
    return;
  }
  await loadProfiles();
  listen<{ state: string; error?: string }>('tunnel-status', (e) => {
    conn.tunnel = e.payload.state;
    if (e.payload.state === 'up') openWs();
  });
  const info = await invoke<ProxyInfo>('proxy_info');
  setTransport(proxyTransport(info.url, info.session, info.connected));
  applyProxyInfo(info);
  if (!conn.activeId || !conn.profiles.length) {
    conn.state = 'unconfigured';
    return;
  }
  await connect(conn.activeId);
}

export async function connect(id: string) {
  conn.state = 'connecting';
  conn.lastError = null;
  stopWs();
  if (!isTauri) return;
  try {
    const info = await invoke<ProxyInfo>('connect', { id });
    setTransport(proxyTransport(info.url, info.session, info.connected));
    applyProxyInfo(info);
    conn.activeId = id;
    await refreshSystem();
    startWs();
    if ((conn.state as ConnState) !== 'online') conn.state = 'offline';
  } catch (e) {
    conn.lastError = String(e);
    conn.state = 'offline';
    void refreshTransportInfo();
    // Keep trying in the background; the engine may simply be starting.
    startWs();
  }
}

export async function disconnect() {
  stopWs();
  if (isTauri) await invoke('disconnect');
  conn.state = conn.profiles.length ? 'offline' : 'unconfigured';
  conn.system = null;
  conn.snp = null;
}

export async function saveProfile(profile: Profile, token?: string) {
  const saved = await invoke<Profile>('save_profile', { profile, token: token || null });
  await loadProfiles();
  return saved;
}

export async function deleteProfile(id: string) {
  await invoke('delete_profile', { id });
  await loadProfiles();
  if (!conn.profiles.length) {
    stopWs();
    conn.state = 'unconfigured';
  }
}

export async function testConnection(profile: Profile, token?: string) {
  return invoke<TestResult>('test_connection', { profile, token: token || null });
}

/** Trust-on-first-use: asks the server for its identity without authenticating. */
export async function probePin(profile: Profile) {
  return invoke<{ pin: string; fingerprint: string }>('snp_probe_pin', { profile });
}

export async function reconnectNow() {
  if (conn.activeId) await connect(conn.activeId);
}
