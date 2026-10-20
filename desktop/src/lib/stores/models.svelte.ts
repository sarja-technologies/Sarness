// Models of every connected engine: one unified list for pickers (each model tagged with the server
// that hosts it) and per-engine lists for management. A model installed on two servers appears once
// in pickers; the router decides where it runs.

import { apiFor } from '../api/client';
import { isTauri } from '../api/transport';
import type { CatalogEntry, ChatModel, Download, InstalledModel, LlmStatus, ModelCategory, NodeLibrary } from '../api/types';
import { conn, onEvent, profileName } from './connection.svelte';
import { toast } from './toast.svelte';

export interface EngineModels {
  installed: InstalledModel[];
  downloads: Download[];
  chatModels: ChatModel[];
  llm: LlmStatus | null;
  loaded: boolean;
}

export const models = $state({
  /** Installed models of every engine (a model on two servers appears twice, with different `engine`). */
  installed: [] as InstalledModel[],
  /** Catalog of `catalogEngine`. */
  catalog: [] as CatalogEntry[],
  catalogEngine: null as string | null,
  downloads: [] as Download[],
  /** Chat models of every engine, tagged with `engine`. */
  chatModels: [] as ChatModel[],
  /** LLM runtime status of the primary engine. */
  llm: null as LlmStatus | null,
  /** Node library of `nodesEngine`. */
  nodes: null as NodeLibrary | null,
  nodesEngine: null as string | null,
  loaded: false,
  by: {} as Record<string, EngineModels>,
});

/** The id used to address an engine through the proxy (browser development has no prefix). */
const target = (engine: string | null | undefined) => (isTauri ? engine ?? null : null);

function rebuild() {
  const order = [conn.activeId, ...Object.keys(models.by).filter((id) => id !== conn.activeId)].filter((id): id is string => !!id && !!models.by[id]);
  models.installed = order.flatMap((id) => models.by[id].installed);
  models.chatModels = order.flatMap((id) => models.by[id].chatModels);
  models.downloads = order.flatMap((id) => models.by[id].downloads).sort((a, b) => b.created_at - a.created_at);
  models.llm = (conn.activeId && models.by[conn.activeId]?.llm) || null;
  models.loaded = order.some((id) => models.by[id].loaded);
}

/** Distinct by key, keeping the first (primary engine first). */
function distinct<T>(list: T[], key: (t: T) => string): T[] {
  const seen = new Set<string>();
  return list.filter((x) => (seen.has(key(x)) ? false : (seen.add(key(x)), true)));
}

/** Installed models of a category for pickers: one entry per model across servers, or one server's. */
export function byCategory(cat: ModelCategory, supportedOnly = true, engine?: string | null): InstalledModel[] {
  const list = models.installed.filter((m) => m.category === cat && (!supportedOnly || m.supported) && (!engine || m.engine === engine));
  return engine ? list : distinct(list, (m) => m.id);
}

/** Distinct chat models across servers, for pickers. */
export function chatOptions(): ChatModel[] {
  return distinct(models.chatModels, (m) => m.ref);
}

export function modelById(id: string | null | undefined, engine?: string | null): InstalledModel | undefined {
  if (!id) return undefined;
  return (engine && models.by[engine]?.installed.find((m) => m.id === id)) || models.installed.find((m) => m.id === id);
}

export function chatModelByRef(ref: string | null | undefined, engine?: string | null): ChatModel | undefined {
  if (!ref) return undefined;
  return (engine && models.by[engine]?.chatModels.find((m) => m.ref === ref)) || models.chatModels.find((m) => m.ref === ref);
}

/** Servers (profile ids) that have a model: image/video by id, chat by ref. */
export function hostsOf(key: string | null | undefined, kind: 'chat' | 'media'): string[] {
  if (!key) return [];
  return Object.entries(models.by)
    .filter(([, m]) => (kind === 'chat' ? m.chatModels.some((c) => c.ref === key) : m.installed.some((x) => x.id === key && x.supported)))
    .map(([id]) => id);
}

/** "on Spark" / "on RTX · Spark": the host servers of a model, for picker hints (empty with one server). */
export function hostHint(key: string | null | undefined, kind: 'chat' | 'media'): string {
  if (Object.keys(models.by).length < 2) return '';
  const hosts = hostsOf(key, kind);
  return hosts.length ? `on ${hosts.map(profileName).join(' · ')}` : '';
}

export function llmOn(engine: string | null | undefined): LlmStatus | null {
  return (engine && models.by[engine]?.llm) || null;
}

const tagged = <T extends object>(list: T[], engine: string) => list.map((x) => ({ ...x, engine }));

/** Loads one engine's models, or every connected engine's when `engine` is omitted. */
export async function loadModels(engine?: string | null) {
  const ids = engine ? [engine] : Object.keys(conn.engines).filter((id) => conn.engines[id].state === 'online');
  // Nothing to load: leave the lists untouched. Rebuilding replaces them, which re-runs every effect that
  // reads them; an effect that loads when they are empty would then spin forever before any server connects.
  if (!ids.length) return;
  await Promise.all(ids.map(async (id) => {
    const a = apiFor(target(id));
    const [installed, downloads, chat, llm] = await Promise.all([
      a.get<InstalledModel[]>('/v1/models'),
      a.get<Download[]>('/v1/downloads'),
      a.get<ChatModel[]>('/v1/llm/models'),
      a.get<LlmStatus>('/v1/llm/status'),
    ]);
    if (!conn.engines[id]) return; // disconnected meanwhile
    models.by[id] = { installed: tagged(installed, id), downloads: tagged(downloads, id), chatModels: tagged(chat, id), llm, loaded: true };
  }));
  rebuild();
}

export async function loadCatalog(engine?: string | null) {
  const id = engine ?? models.catalogEngine ?? conn.activeId;
  models.catalog = await apiFor(target(id)).get<CatalogEntry[]>('/v1/models/catalog');
  models.catalogEngine = id;
}

/** Node library of one engine (its nodes list that engine's models and families). */
export async function loadNodeLibrary(force = false, engine?: string | null) {
  const id = engine ?? conn.activeId;
  if (models.nodes && !force && models.nodesEngine === id) return models.nodes;
  models.nodes = await apiFor(target(id)).get<NodeLibrary>('/v1/nodes');
  models.nodesEngine = id;
  return models.nodes;
}

export async function installCatalog(catalog_id: string, quant?: string, engine?: string | null) {
  const id = engine ?? models.catalogEngine ?? conn.activeId;
  const d = await apiFor(target(id)).post<Download>('/v1/models/install', { catalog_id, quant });
  if (id) upsertDownload({ ...d, engine: id });
  toast('info', 'Download started', `${d.name}${Object.keys(conn.engines).length > 1 ? ` on ${profileName(id)}` : ''}`);
  return d;
}

export function upsertDownload(d: Partial<Download> & { id: string; engine?: string }) {
  const id = d.engine ?? conn.activeId;
  const m = id ? models.by[id] : undefined;
  if (!m) return;
  const i = m.downloads.findIndex((x) => x.id === d.id);
  if (i >= 0) m.downloads[i] = { ...m.downloads[i], ...d, engine: id! };
  else m.downloads.unshift({ ...(d as Download), engine: id! });
  rebuild();
}

export function dropDownload(engine: string, id: string) {
  const m = models.by[engine];
  if (!m) return;
  m.downloads = m.downloads.filter((x) => x.id !== id);
  rebuild();
}

let wired = false;
export function wireModelEvents() {
  if (wired) return;
  wired = true;
  onEvent('download.progress', (e) => {
    upsertDownload({ id: e.data.id, done_bytes: e.data.done_bytes, total_bytes: e.data.total_bytes, speed_bps: e.data.speed_bps, status: 'downloading', engine: e.engine });
  });
  for (const t of ['download.created', 'download.updated']) {
    onEvent(t, (e) => {
      if (!e.data?.id) return;
      upsertDownload({ ...e.data, engine: e.engine });
      const where = Object.keys(conn.engines).length > 1 ? ` on ${profileName(e.engine)}` : '';
      if (e.data.status === 'completed') toast('success', `Model installed${where}`, e.data.name);
      if (e.data.status === 'failed') toast('error', `Download failed${where}`, e.data.error);
    });
  }
  onEvent('models.changed', (e) => {
    loadModels(e.engine).catch(() => {});
    if (models.catalog.length && (!e.engine || e.engine === models.catalogEngine)) loadCatalog().catch(() => {});
  });
  onEvent('llm.status', (e) => {
    const id = e.engine ?? conn.activeId;
    if (id && models.by[id]) models.by[id].llm = e.data;
    rebuild();
  });
  onEvent('connection.restored', (e) => loadModels(e.engine).catch(() => {}));
  onEvent('engine.primary', () => rebuild());
  onEvent('engine.disconnected', (e) => {
    if (e.engine) delete models.by[e.engine];
    if (models.catalogEngine === e.engine) models.catalogEngine = null;
    if (models.nodesEngine === e.engine) {
      models.nodes = null;
      models.nodesEngine = null;
    }
    rebuild();
  });
}
