// Server connections: profiles, the desktop proxy, one live event stream and health per engine.
//
// Several engines can be connected at once. Each is addressed by its profile id; the desktop proxy
// routes `/e/<id>/v1/...` to it. One of them is the primary, which also serves un-prefixed `/v1/...`
// requests; the single-engine fields of `conn` (state, system, stats, …) mirror the primary so views
// written for one server keep working.

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { apiFor } from '../api/client';
import { devTransport, isTauri, proxyTransport, setTransport, transport, wsUrl } from '../api/transport';
import type { Capabilities, EngineEvent, LiveStats, SystemInfo } from '../api/types';
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
  /** Workloads this server is preferred for: chat, code, vision, image, video. */
  preferred_tasks?: string[];
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

type SnpLive = { status: { state: string; error: string | null; error_kind: string | null; fatal: boolean }; session: (SnpNegotiated & { session?: string }) | null };

export interface ProxyInfo {
  url: string;
  session: string;
  connected: boolean;
  /** The primary engine. */
  profile_id: string | null;
  transport: Transport | null;
  snp: SnpLive | null;
  engines?: { profile_id: string; primary: boolean; transport: Transport; snp: SnpLive | null }[];
}

export type ConnState = 'booting' | 'unconfigured' | 'connecting' | 'online' | 'offline';

/** Live state of one connected engine. */
export interface EngineConn {
  id: string;
  state: 'connecting' | 'online' | 'offline';
  system: SystemInfo | null;
  caps: Capabilities | null;
  stats: LiveStats | null;
  lastError: string | null;
  wsConnected: boolean;
  lastEventAt: number;
  tunnel: string | null;
  transport: Transport | null;
  snp: SnpLive | null;
}

export const conn = $state({
  state: 'booting' as ConnState,
  profiles: [] as Profile[],
  /** The primary engine's profile id. */
  activeId: null as string | null,
  /** Every connected engine, by profile id. */
  engines: {} as Record<string, EngineConn>,
  // Mirrors of the primary engine.
  system: null as SystemInfo | null,
  stats: null as LiveStats | null,
  lastError: null as string | null,
  wsConnected: false,
  tunnel: null as string | null,
  lastEventAt: 0,
  transport: null as Transport | null,
  snp: null as ProxyInfo['snp'],
});

export const activeProfile = () => conn.profiles.find((p) => p.id === conn.activeId) ?? null;
export const profileById = (id: string | null | undefined) => (id ? conn.profiles.find((p) => p.id === id) ?? null : null);
export const profileName = (id: string | null | undefined) => profileById(id)?.name ?? (id ? 'Disconnected server' : 'Server');

/** Connected engines, primary first. */
export function engineList(): EngineConn[] {
  return Object.values(conn.engines).sort((a, b) => (a.id === conn.activeId ? -1 : b.id === conn.activeId ? 1 : profileName(a.id).localeCompare(profileName(b.id))));
}
export const onlineEngines = () => engineList().filter((e) => e.state === 'online');
export const engineOnline = (id: string | null | undefined) => !!id && conn.engines[id]?.state === 'online';

function syncPrimary() {
  const e = conn.activeId ? conn.engines[conn.activeId] : undefined;
  conn.system = e?.system ?? null;
  conn.stats = e?.stats ?? null;
  conn.wsConnected = e?.wsConnected ?? false;
  conn.lastEventAt = e?.lastEventAt ?? 0;
  conn.tunnel = e?.tunnel ?? null;
  conn.transport = e?.transport ?? null;
  conn.snp = e?.snp ?? null;
  if (e) {
    conn.lastError = e.lastError;
    conn.state = e.state;
  } else if (conn.state !== 'booting') {
    conn.state = conn.profiles.length ? 'offline' : 'unconfigured';
  }
}

function engine(id: string): EngineConn {
  if (!conn.engines[id]) {
    conn.engines[id] = { id, state: 'connecting', system: null, caps: null, stats: null, lastError: null, wsConnected: false, lastEventAt: 0, tunnel: null, transport: null, snp: null };
  }
  return conn.engines[id];
}

function applyProxyInfo(info: ProxyInfo) {
  setTransport(proxyTransport(info.url, info.session, info.connected));
  conn.activeId = info.profile_id ?? conn.activeId;
  for (const l of info.engines ?? []) {
    const e = conn.engines[l.profile_id];
    if (!e) continue;
    e.transport = l.transport;
    e.snp = l.snp;
  }
  syncPrimary();
}

/** Re-reads transports and negotiated SNP details from the desktop core. */
export async function refreshTransportInfo() {
  if (!isTauri) return;
  try {
    applyProxyInfo(await invoke<ProxyInfo>('proxy_info'));
  } catch {
    /* informational only */
  }
}

type Handler = (e: EngineEvent) => void;
const handlers = new Map<string, Set<Handler>>();

/** Subscribe to engine events by type ("job.updated") or prefix ("job.*"). `e.engine` names the sender. */
export function onEvent(type: string, fn: Handler): () => void {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type)!.add(fn);
  return () => handlers.get(type)?.delete(fn);
}

function dispatch(e: EngineEvent) {
  handlers.get(e.type)?.forEach((h) => h(e));
  const prefix = e.type.split('.')[0] + '.*';
  handlers.get(prefix)?.forEach((h) => h(e));
  handlers.get('*')?.forEach((h) => h(e));
}

// ------------------------------------------------------------- websocket, one per engine
class Link {
  ws: WebSocket | null = null;
  timer: ReturnType<typeof setTimeout> | null = null;
  watchdog: ReturnType<typeof setInterval> | null = null;
  backoff = 1000;
  want = false;
  constructor(readonly id: string) {}

  open() {
    if (!this.want || !transport().base) return;
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    const id = this.id;
    // In browser development there is a single engine without a proxy prefix.
    const sock = new WebSocket(wsUrl('/v1/events', isTauri ? id : null));
    this.ws = sock;
    sock.onopen = () => {
      const e = conn.engines[id];
      if (!e) return;
      trace(`event stream connected (${profileName(id)})`);
      this.backoff = 1000;
      e.wsConnected = true;
      e.lastEventAt = Date.now();
      if (e.state === 'offline') {
        e.state = 'online';
        void refreshEngine(id);
        dispatch({ type: 'connection.restored', data: {}, engine: id });
      }
      if (id === conn.activeId) syncPrimary();
    };
    sock.onmessage = (m) => {
      const e = conn.engines[id];
      if (!e) return;
      e.lastEventAt = Date.now();
      try {
        const ev = JSON.parse(m.data) as EngineEvent;
        ev.engine = id;
        if (ev.type === 'system.stats') e.stats = ev.data;
        if (id === conn.activeId) {
          conn.lastEventAt = e.lastEventAt;
          if (ev.type === 'system.stats') conn.stats = ev.data;
        }
        dispatch(ev);
      } catch {
        /* ignore malformed */
      }
    };
    sock.onclose = () => {
      if (this.ws !== sock) return;
      const e = conn.engines[id];
      if (!e) return;
      e.wsConnected = false;
      if (this.want) {
        if (e.state === 'online') {
          e.state = 'offline';
          dispatch({ type: 'connection.lost', data: {}, engine: id });
        }
        this.schedule();
      }
      if (id === conn.activeId) syncPrimary();
    };
    sock.onerror = () => sock.close();
  }

  schedule() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.open(), this.backoff);
    this.backoff = Math.min(this.backoff * 1.7, 15000);
  }

  start() {
    this.want = true;
    this.open();
    if (this.watchdog) clearInterval(this.watchdog);
    // Heartbeats arrive every 15s; silence for 40s means a dead connection.
    this.watchdog = setInterval(() => {
      const e = conn.engines[this.id];
      if (e?.wsConnected && Date.now() - e.lastEventAt > 40000) {
        e.wsConnected = false;
        this.ws?.close();
        if (e.state === 'online') dispatch({ type: 'connection.lost', data: {}, engine: this.id });
        e.state = 'offline';
        if (this.id === conn.activeId) syncPrimary();
        this.schedule();
      }
    }, 5000);
  }

  stop() {
    this.want = false;
    if (this.timer) clearTimeout(this.timer);
    if (this.watchdog) clearInterval(this.watchdog);
    this.ws?.close();
    this.ws = null;
  }
}

const links = new Map<string, Link>();

function startWs(id: string) {
  let l = links.get(id);
  if (!l) links.set(id, (l = new Link(id)));
  l.start();
}

function stopWs(id: string) {
  links.get(id)?.stop();
  links.delete(id);
}

// ---------------------------------------------------------------- actions
function trace(msg: string) {
  (window as any).__sarnessLog?.('info', msg);
}

/** Capabilities of an engine that predates `/v1/capabilities`, from its system document. */
export function derivedCapabilities(sys: SystemInfo): Capabilities {
  const g = sys.hardware.gpus[0];
  return {
    server_id: '', engine_version: sys.engine.version, api_version: sys.engine.api_version, hostname: sys.hardware.hostname,
    arch: sys.hardware.arch ?? '', os: sys.hardware.distro, cpu: sys.hardware.cpu.name,
    gpu: g ? { name: g.name, compute_capability: g.compute_capability ?? null, unified_memory: !!g.unified_memory, memory_total_mb: g.vram_total_mb, memory_free_mb: g.vram_free_mb, utilization_pct: g.utilization_pct ?? null, driver: g.driver_version ?? null, cuda: g.cuda_driver_version ?? null } : null,
    memory: sys.hardware.memory, storage_free_gb: sys.hardware.storage.free_gb,
    backends: [sys.runtime.llama_cpp ? 'llama.cpp' : '', sys.runtime.diffusers ? 'diffusers' : ''].filter(Boolean),
    experiments: [], chat_endpoints: [], models: [],
    llm: { state: sys.llm?.state ?? 'stopped', model_id: sys.llm?.model_id ?? null, active_streams: sys.llm?.active_streams ?? 0 },
    jobs: { running: sys.jobs.running ?? 0, queued: sys.jobs.queued ?? 0, concurrency: 1, queue_soft_limit: 8, busy_reason: null },
    features: [], health: sys.health, generated_at: Date.now() / 1000, derived: true,
  };
}

/** Reloads one engine's system and capabilities documents. */
export async function refreshEngine(id: string) {
  const e = conn.engines[id];
  if (!e) return null;
  const a = apiFor(isTauri ? id : null);
  try {
    e.system = await a.get<SystemInfo>('/v1/system');
    trace(`system info loaded from ${e.system.hardware.hostname} (${profileName(id)})`);
    try {
      e.caps = await a.get<Capabilities>('/v1/capabilities');
    } catch (err) {
      // Older engines: same information, assembled from the system document.
      if ((err as { status?: number }).status !== 404) throw err;
      e.caps = derivedCapabilities(e.system);
    }
    e.lastError = null;
    if (e.state !== 'online') e.state = 'online';
  } catch (err) {
    e.lastError = (err as Error).message;
    (window as any).__sarnessLog?.('warn', `system request to ${profileName(id)} failed: ${e.lastError}`);
    if (e.state === 'online' || e.state === 'connecting') e.state = 'offline';
  }
  if (id === conn.activeId) syncPrimary();
  return e.system;
}

/** Reloads the primary engine (kept for single-engine callers). */
export async function refreshSystem() {
  if (conn.activeId) await refreshEngine(conn.activeId);
  void refreshTransportInfo();
  return conn.system;
}

/** Re-reads capabilities of every online engine (load, free memory, loaded models). */
export async function refreshCapabilities() {
  await Promise.allSettled(onlineEngines().map(async (e) => {
    try {
      e.caps = await apiFor(isTauri ? e.id : null).get<Capabilities>('/v1/capabilities');
    } catch {
      /* older engine or offline: keep the last document */
    }
  }));
}

export async function loadProfiles() {
  if (!isTauri) return { connected: [] as string[] };
  const store = await invoke<{ profiles: Profile[]; active: string | null; connected?: string[] }>('list_profiles');
  conn.profiles = store.profiles;
  if (!Object.keys(conn.engines).length) conn.activeId = store.active;
  return { connected: store.connected ?? (store.active ? [store.active] : []) };
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
    conn.activeId = 'dev';
    engine('dev').transport = 'http';
    conn.state = 'connecting';
    await refreshEngine('dev');
    startWs('dev');
    dispatch({ type: 'connection.restored', data: {}, engine: 'dev' });
    return;
  }
  const { connected } = await loadProfiles();
  listen<{ state: string; error?: string; profile_id?: string }>('tunnel-status', (e) => {
    const id = e.payload.profile_id ?? conn.activeId;
    const en = id ? conn.engines[id] : undefined;
    if (!en) return;
    en.tunnel = e.payload.state;
    if (id === conn.activeId) conn.tunnel = en.tunnel;
    if (e.payload.state === 'up') links.get(en.id)?.open();
  });
  const info = await invoke<ProxyInfo>('proxy_info');
  setTransport(proxyTransport(info.url, info.session, info.connected));
  let ids = connected.filter((id) => conn.profiles.some((p) => p.id === id));
  // Saved servers with a token are never left disconnected at start-up: an emptied auto-connect list
  // (every server disconnected in an earlier session) would otherwise look like being logged out.
  if (!ids.length) ids = conn.profiles.filter((p) => p.has_token).map((p) => p.id);
  if (!ids.length || !conn.profiles.length) {
    conn.state = 'unconfigured';
    return;
  }
  // Primary first, so it keeps serving un-prefixed requests.
  const primary = conn.activeId && ids.includes(conn.activeId) ? conn.activeId : ids[0];
  await connect(primary);
  await Promise.allSettled(ids.filter((id) => id !== primary).map((id) => connect(id)));
}

/** Connects one engine (others stay connected). The first connected engine becomes the primary. */
export async function connect(id: string) {
  const e = engine(id);
  e.state = 'connecting';
  e.lastError = null;
  stopWs(id);
  if (!conn.activeId || !conn.engines[conn.activeId] || conn.activeId === id) conn.activeId = id;
  syncPrimary();
  if (!isTauri) return;
  try {
    applyProxyInfo(await invoke<ProxyInfo>('connect', { id }));
    await refreshEngine(id);
    startWs(id);
    // refreshEngine marks the engine online when it answers.
    if (conn.engines[id]?.state === 'online') dispatch({ type: 'connection.restored', data: {}, engine: id });
    else e.state = 'offline';
  } catch (err) {
    e.lastError = String(err);
    e.state = 'offline';
    void refreshTransportInfo();
    // Keep trying in the background; the engine may simply be starting.
    startWs(id);
  }
  if (id === conn.activeId) syncPrimary();
}

/** Disconnects one engine, or every engine when `id` is omitted. */
export async function disconnect(id?: string) {
  const ids = id ? [id] : Object.keys(conn.engines);
  for (const i of ids) stopWs(i);
  if (isTauri) await invoke('disconnect', { id: id ?? null });
  for (const i of ids) {
    delete conn.engines[i];
    dispatch({ type: 'engine.disconnected', data: {}, engine: i });
  }
  if (isTauri && Object.keys(conn.engines).length) await refreshTransportInfo();
  else if (!Object.keys(conn.engines).length) {
    conn.state = 'booting'; // let syncPrimary pick offline/unconfigured
    syncPrimary();
  } else syncPrimary();
}

/** Makes `id` the primary engine (un-prefixed requests, default for new work). */
export async function setPrimary(id: string) {
  if (!conn.engines[id]) return;
  if (isTauri) applyProxyInfo(await invoke<ProxyInfo>('set_primary', { id }));
  else {
    conn.activeId = id;
    syncPrimary();
  }
  dispatch({ type: 'engine.primary', data: {}, engine: id });
}

export async function saveProfile(profile: Profile, token?: string) {
  const saved = await invoke<Profile>('save_profile', { profile, token: token || null });
  await loadProfiles();
  return saved;
}

export async function deleteProfile(id: string) {
  stopWs(id);
  await invoke('delete_profile', { id });
  if (conn.engines[id]) {
    delete conn.engines[id];
    dispatch({ type: 'engine.disconnected', data: {}, engine: id });
  }
  await loadProfiles();
  if (Object.keys(conn.engines).length) await refreshTransportInfo();
  if (!conn.profiles.length) conn.state = 'unconfigured';
  else syncPrimary();
}

export async function testConnection(profile: Profile, token?: string) {
  return invoke<TestResult>('test_connection', { profile, token: token || null });
}

/** Trust-on-first-use: asks the server for its identity without authenticating. */
export async function probePin(profile: Profile) {
  return invoke<{ pin: string; fingerprint: string }>('snp_probe_pin', { profile });
}

/** Reconnects every engine that is offline. */
export async function reconnectNow(id?: string) {
  const ids = id ? [id] : Object.values(conn.engines).filter((e) => e.state !== 'online').map((e) => e.id);
  await Promise.allSettled(ids.map((i) => connect(i)));
}
