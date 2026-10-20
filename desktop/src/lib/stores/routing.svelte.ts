// Live routing: feeds the pure router (router.ts) with the state of every connected engine, and moves
// inputs (uploaded files, conversations) to the server that will run a request. Jobs themselves are
// never moved: once a server accepted a job, it finishes there.

import { apiFor, type Upload } from '../api/client';
import { isTauri, mediaUrl, transport } from '../api/transport';
import type { Conversation } from '../api/types';
import { conn, engineList, profileById } from './connection.svelte';
import { jobs } from './jobs.svelte';
import { models } from './models.svelte';
import { route, type EngineView, type RouteResult, type RoutingPolicy, type Task } from './router';

const KEY = 'sarness.routing.v1';

function load(): RoutingPolicy {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '{}');
    return {
      modelPrefs: raw && typeof raw.modelPrefs === 'object' && raw.modelPrefs ? raw.modelPrefs : {},
      fallback: raw?.fallback === 'preferred' ? 'preferred' : 'any',
    };
  } catch {
    return { modelPrefs: {}, fallback: 'any' };
  }
}

export const routing = $state(load());

export function persistRouting() {
  try {
    localStorage.setItem(KEY, JSON.stringify($state.snapshot(routing)));
  } catch {
    /* storage unavailable */
  }
}

export function setModelPreference(model: string, engine: string | null) {
  if (engine) routing.modelPrefs[model] = engine;
  else delete routing.modelPrefs[model];
  persistRouting();
}

/** Current state of every connected engine, as the router sees it. */
export function engineViews(): EngineView[] {
  return engineList().map((e) => {
    const m = models.by[e.id];
    const caps = e.caps;
    const installed = m?.installed.filter((x) => x.supported) ?? [];
    const loaded = new Set<string>(caps?.models.filter((x) => x.loaded).map((x) => x.id) ?? []);
    const llm = m?.llm;
    if (llm?.state === 'ready' && llm.model_id) loaded.add(`local:${llm.model_id}`);
    for (const x of caps?.models ?? []) if (x.loaded && x.chat_ref) loaded.add(x.chat_ref);
    const gpu = caps?.gpu;
    return {
      id: e.id,
      name: profileById(e.id)?.name ?? e.id,
      online: e.state === 'online',
      primary: e.id === conn.activeId,
      preferredTasks: profileById(e.id)?.preferred_tasks ?? [],
      models: new Set([...installed.map((x) => x.id), ...(m?.chatModels.map((c) => c.ref) ?? [])]),
      loaded,
      freeMb: gpu ? (gpu.unified_memory ? caps!.memory.available_mb : gpu.memory_free_mb) : null,
      modelMb: Object.fromEntries(installed.map((x) => [x.id, x.vram_mb ?? null])),
      jobsRunning: caps?.jobs.running ?? 0,
      jobsQueued: caps?.jobs.queued ?? 0,
      queueSoftLimit: caps?.jobs.queue_soft_limit ?? 8,
      llmStreams: llm?.active_streams ?? caps?.llm.active_streams ?? 0,
    };
  });
}

/** Where a request runs. Never substitutes the model; explains why when no server can run it. */
export function routeFor(task: Task, model: string | null | undefined, manual?: string | null): RouteResult {
  if (!model) return { ok: false, error: 'Choose a model first.' };
  // Browser development: a single engine without the multi-engine proxy.
  if (!isTauri) return conn.activeId ? { ok: true, engine: conn.activeId, reason: 'development engine', candidates: [conn.activeId] } : { ok: false, error: 'Not connected to a server.' };
  const views = engineViews();
  // Live job counts from events are fresher than the last capabilities document.
  for (const v of views) {
    const mine = Object.values(jobs.byId).filter((j) => j.engine === v.id);
    if (mine.length) {
      v.jobsRunning = mine.filter((j) => j.status === 'running').length;
      v.jobsQueued = mine.filter((j) => j.status === 'queued').length;
    }
  }
  return route({ task, model, manual }, views, $state.snapshot(routing) as RoutingPolicy, jobs.perf);
}

const target = (engine: string | null | undefined) => (isTauri ? engine ?? null : null);

/**
 * Makes an uploaded file available on `to`. Files live on the server they were uploaded to; when work
 * runs elsewhere the desktop copies the file across (through the proxy) and returns the new id.
 */
export async function fileOn(fileId: string, from: string | null | undefined, to: string | null | undefined): Promise<string> {
  if (!from || !to || from === to || !isTauri) return fileId;
  const res = await fetch(mediaUrl(`/v1/files/${fileId}`, from), { headers: transport().headers });
  if (!res.ok) throw new Error(`Could not copy the input file to the other server (${res.status})`);
  const blob = await res.blob();
  const ext = (blob.type.split('/')[1] ?? 'bin').replace('jpeg', 'jpg');
  const up: Upload = await apiFor(target(to)).upload(blob, `${fileId}.${ext}`);
  return up.id;
}

/**
 * Copies a conversation to another server (export → import), so a model on that server can continue
 * it. The original stays where it was. Attachments are not carried over (the engine export omits them).
 */
export async function moveConversation(convId: string, from: string, to: string): Promise<Conversation> {
  const data = await apiFor(target(from)).get<Record<string, unknown>>(`/v1/chat/conversations/${convId}/export`);
  return apiFor(target(to)).post<Conversation>('/v1/chat/import', data);
}
