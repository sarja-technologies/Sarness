import { ApiError, apiFor, qs } from '../api/client';
import { isTauri } from '../api/transport';
import type { Generation, Job, NodeState, Workflow } from '../api/types';
import { conn, onEvent } from './connection.svelte';
import { recordPerf, type PerfTable } from './router';
import { toast } from './toast.svelte';

interface LiveProgress {
  progress: number;
  text: string | null;
  node_id: string | null;
  step: number | null;
  total: number | null;
}

// Job ids are random per engine, so they stay unique across servers; each job records its `engine`.
export const jobs = $state({
  byId: {} as Record<string, Job>,
  progress: {} as Record<string, LiveProgress>,
  previews: {} as Record<string, string>,
  nodeStates: {} as Record<string, Record<string, NodeState>>,
  loaded: false,
  /** Observed seconds per run, by engine and model id (feeds the router). */
  perf: loadPerf(),
});

const PERF_KEY = 'sarness.routing.perf.v1';
function loadPerf(): PerfTable {
  try {
    return JSON.parse(localStorage.getItem(PERF_KEY) ?? '{}') ?? {};
  } catch {
    return {};
  }
}

const target = (engine: string | null | undefined) => (isTauri ? engine ?? null : null);
/** The engine that holds a job (older entries without one belong to the primary). */
export const jobEngine = (id: string) => jobs.byId[id]?.engine ?? conn.activeId;

/** Callbacks fired when a generation is created (views append to their history). */
const genListeners = new Set<(g: Generation) => void>();
export function onGeneration(fn: (g: Generation) => void) {
  genListeners.add(fn);
  return () => genListeners.delete(fn);
}

export function activeJobs(kind?: 'image' | 'video', engine?: string | null): Job[] {
  return Object.values(jobs.byId)
    .filter((j) => (j.status === 'queued' || j.status === 'running') && (!kind || j.kind === kind) && (!engine || j.engine === engine))
    .sort((a, b) => a.created_at - b.created_at);
}

export function recentJobs(limit = 30): Job[] {
  return Object.values(jobs.byId).sort((a, b) => b.created_at - a.created_at).slice(0, limit);
}

/** Loads recent jobs of one engine, or of every connected engine. */
export async function loadJobs(engine?: string | null) {
  const ids = engine ? [engine] : Object.keys(conn.engines).filter((id) => conn.engines[id].state === 'online');
  await Promise.all(ids.map(async (id) => {
    const list = await apiFor(target(id)).get<Job[]>(`/v1/jobs${qs({ limit: 80 })}`);
    for (const j of list) jobs.byId[j.id] = { ...j, engine: id };
  }));
  jobs.loaded = true;
}

const newKey = () => `ck_${crypto.randomUUID().replace(/-/g, '')}`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Submits a job to `engine` (default: primary). Engines that support idempotent submission get a
 * client key, so a submission that is retried after a network interruption never runs twice; older
 * engines get exactly one attempt.
 */
export async function submitJob(body: {
  kind: 'image' | 'video';
  simple?: Record<string, unknown>;
  workflow?: Workflow;
  project_id?: string | null;
  folder_id?: string | null;
  title?: string;
}, engine?: string | null): Promise<Job> {
  const id = engine ?? conn.activeId;
  const a = apiFor(target(id));
  const idempotent = !!(id && conn.engines[id]?.caps?.features?.includes('job_client_key'));
  const payload = idempotent ? { ...body, client_key: newKey() } : body;
  let job: Job | null = null;
  for (let attempt = 0; ; attempt++) {
    try {
      job = await a.post<Job>('/v1/jobs', payload);
      break;
    } catch (e) {
      const transient = e instanceof ApiError && e.offline;
      if (!idempotent || !transient || attempt >= 4) throw e;
      await sleep(1500 * (attempt + 1));
    }
  }
  const j = { ...job!, engine: id ?? undefined };
  jobs.byId[j.id] = j;
  return j;
}

export async function cancelJob(id: string) {
  const engine = jobEngine(id);
  const j = await apiFor(target(engine)).post<Job>(`/v1/jobs/${id}/cancel`);
  jobs.byId[id] = { ...jobs.byId[id], ...j, engine: engine ?? undefined };
}

/** Retries on the server that ran the job (jobs are never moved between servers). */
export async function retryJob(id: string, newSeed = false) {
  const engine = jobEngine(id);
  const j = await apiFor(target(engine)).post<Job>(`/v1/jobs/${id}/retry`, { new_seed: newSeed });
  jobs.byId[j.id] = { ...j, engine: engine ?? undefined };
  return jobs.byId[j.id];
}

function notePerf(j: Job) {
  const model = (j.simple as { model?: string } | null)?.model;
  if (!j.engine || !model || !j.result?.elapsed_s) return;
  jobs.perf = recordPerf(jobs.perf, j.engine, model, j.result.elapsed_s);
  try {
    localStorage.setItem(PERF_KEY, JSON.stringify(jobs.perf));
  } catch {
    /* storage unavailable */
  }
}

let wired = false;
export function wireJobEvents() {
  if (wired) return;
  wired = true;
  const engineOf = (e: { engine?: string }) => e.engine ?? conn.activeId ?? undefined;
  onEvent('job.created', (e) => {
    jobs.byId[e.data.id] = { ...(jobs.byId[e.data.id] ?? {}), ...e.data, engine: engineOf(e) };
  });
  onEvent('job.updated', (e) => {
    const j = { ...(e.data as Job), engine: engineOf(e) };
    const prev = jobs.byId[j.id];
    jobs.byId[j.id] = { ...(prev ?? {}), ...j };
    if (j.status !== 'running') {
      delete jobs.progress[j.id];
    }
    if (j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') {
      delete jobs.previews[j.id];
      if (j.node_states) jobs.nodeStates[j.id] = j.node_states;
    }
    if (prev && prev.status !== j.status && j.status === 'completed') notePerf(jobs.byId[j.id]);
    if (prev && prev.status !== j.status && j.status === 'failed') {
      toast('error', `${j.kind === 'video' ? 'Video' : 'Image'} generation failed`, j.error?.message ?? undefined);
    }
  });
  onEvent('job.progress', (e) => {
    jobs.progress[e.data.id] = e.data;
    const j = jobs.byId[e.data.id];
    if (j) {
      j.progress = e.data.progress;
      j.progress_text = e.data.text;
      if (j.status === 'queued') j.status = 'running';
    }
  });
  onEvent('job.preview', (e) => {
    jobs.previews[e.data.id] = e.data.image;
  });
  onEvent('job.node', (e) => {
    const { id, node_id, ...state } = e.data;
    if (!jobs.nodeStates[id]) jobs.nodeStates[id] = {};
    jobs.nodeStates[id][node_id] = state;
  });
  onEvent('generation.created', (e) => {
    for (const fn of genListeners) fn({ ...(e.data as Generation), engine: engineOf(e) });
  });
  onEvent('connection.restored', (e) => {
    loadJobs(e.engine).catch(() => {});
  });
}
