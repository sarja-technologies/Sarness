// Chooses the server (engine) that runs a request. Pure: no Svelte, no network, so it is unit-tested.
//
// Order of decision:
//   1. manual selection   — a server the user picked for this conversation/model is always honoured;
//   2. compatibility      — only servers that have the requested model (it is never swapped for another);
//   3. availability       — only servers that are online;
//   4. preferred server   — the user's preferred server for this model, then for this workload;
//   5. free compute       — a server that already holds the model, or has room for it;
//   6. queue load         — fewer running/queued jobs (or chat streams);
//   7. observed performance — faster past runs of the same model;
//   8. fallback policy    — "any": fall back to any compatible server; "preferred": only preferred servers.
//
// Requests are routed when they start; work already running on a server is never moved.

export type Task = 'chat' | 'code' | 'vision' | 'image' | 'video';

export interface EngineView {
  id: string;
  name: string;
  online: boolean;
  primary?: boolean;
  /** Workloads the user prefers this server for. */
  preferredTasks: string[];
  /** Model keys (image/video model id, or chat ref) installed and supported on this server. */
  models: Set<string>;
  /** Model keys currently loaded in memory. */
  loaded: Set<string>;
  /** Free GPU memory (system memory on unified-memory GPUs), MiB; null when unknown. */
  freeMb: number | null;
  /** Approximate memory each model needs, MiB, by model key. */
  modelMb?: Record<string, number | null | undefined>;
  jobsRunning: number;
  jobsQueued: number;
  queueSoftLimit: number;
  llmStreams: number;
}

export interface RoutingPolicy {
  /** Preferred server per model key. */
  modelPrefs: Record<string, string>;
  fallback: 'any' | 'preferred';
}

export interface RouteRequest {
  task: Task;
  /** Model key: image/video model id, or the chat model ref. */
  model: string;
  /** A server the user chose explicitly (null/undefined = automatic). */
  manual?: string | null;
}

/** Observed seconds per run of a model, by engine and model key (lower is faster). */
export type PerfTable = Record<string, Record<string, number>>;

export type RouteResult =
  | { ok: true; engine: string; reason: string; candidates: string[] }
  | { ok: false; error: string };

const isMedia = (t: Task) => t === 'image' || t === 'video';

/** Load figure used to compare servers: GPU jobs for media, active chat streams for text. */
function load(e: EngineView, task: Task): number {
  return isMedia(task) ? e.jobsRunning + e.jobsQueued : e.llmStreams + (e.jobsRunning > 0 ? 0.5 : 0);
}

export function route(req: RouteRequest, engines: EngineView[], policy: RoutingPolicy, perf: PerfTable = {}): RouteResult {
  const name = (id: string) => engines.find((e) => e.id === id)?.name ?? 'that server';

  // 1. manual selection: honoured exactly, never redirected.
  if (req.manual) {
    const e = engines.find((x) => x.id === req.manual);
    if (!e) return { ok: false, error: 'The server chosen for this conversation is not connected. Connect it, or switch routing to Automatic.' };
    if (!e.models.has(req.model)) return { ok: false, error: `${e.name} does not have this model installed. Choose a model it has, or switch routing to Automatic.` };
    if (!e.online) return { ok: false, error: `${e.name} is offline. Wait for it to reconnect, or switch routing to Automatic.` };
    return { ok: true, engine: e.id, reason: `chosen manually`, candidates: [e.id] };
  }

  // 2. compatibility
  const compatible = engines.filter((e) => e.models.has(req.model));
  if (!compatible.length) return { ok: false, error: 'No connected server has this model installed.' };
  // 3. availability
  const online = compatible.filter((e) => e.online);
  if (!online.length) {
    const where = compatible.map((e) => e.name).join(', ');
    return { ok: false, error: `This model is only installed on ${where}, which ${compatible.length > 1 ? 'are' : 'is'} offline.` };
  }

  // 4. preferred server (per model, then per workload), with the fallback policy.
  const modelPref = policy.modelPrefs[req.model];
  const isPreferred = (e: EngineView) => e.id === modelPref || e.preferredTasks.includes(req.task);
  const anyPreference = !!modelPref || compatible.some((e) => e.preferredTasks.includes(req.task));
  let pool = online;
  if (policy.fallback === 'preferred' && anyPreference) {
    pool = online.filter(isPreferred);
    if (!pool.length) {
      const pref = compatible.filter(isPreferred).map((e) => e.name).join(', ') || name(modelPref);
      return { ok: false, error: `The preferred server for this work (${pref}) is unavailable, and fallback to other servers is turned off in Settings.` };
    }
  }

  const fits = (e: EngineView) => {
    if (e.loaded.has(req.model)) return 2;
    const need = e.modelMb?.[req.model];
    if (e.freeMb == null || !need) return 1;
    return e.freeMb >= need ? 1 : 0;
  };
  const overloaded = (e: EngineView) => (isMedia(req.task) ? e.jobsRunning + e.jobsQueued >= e.queueSoftLimit : false);

  // Steps 4–7 in order; each only breaks ties left by the previous ones.
  const steps: ((e: EngineView) => number)[] = [
    // A saturated queue loses its preference: better to start now elsewhere than wait.
    (e) => (overloaded(e) ? 0 : 1),
    (e) => (e.id === modelPref ? 1 : 0),
    (e) => (e.preferredTasks.includes(req.task) ? 1 : 0),
    fits,
    (e) => -load(e, req.task),
  ];
  const speed = (e: EngineView) => perf[e.id]?.[req.model];
  const ranked = [...pool].sort((a, b) => {
    for (const f of steps) {
      const d = f(b) - f(a);
      if (d) return d;
    }
    // Observed performance only counts when both servers have run this model before.
    const sa = speed(a);
    const sb = speed(b);
    if (sa != null && sb != null && sa !== sb) return sa - sb;
    if (!!a.primary !== !!b.primary) return a.primary ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  const best = ranked[0];
  const why: string[] = [];
  if (pool.length === 1) why.push(compatible.length === 1 ? 'only server with this model' : 'only available server with this model');
  else {
    if (best.id === modelPref) why.push('preferred for this model');
    else if (best.preferredTasks.includes(req.task)) why.push(`preferred for ${req.task}`);
    if (best.loaded.has(req.model)) why.push('model already loaded');
    else if (fits(best) === 1 && best.freeMb != null) why.push('has free memory');
    if (load(best, req.task) < Math.max(...pool.map((e) => load(e, req.task)))) why.push('least busy');
    const sp = speed(best);
    if (sp != null && pool.some((e) => e !== best && (speed(e) ?? 0) > sp)) why.push('fastest so far');
    if (!why.length) why.push(best.primary ? 'primary server' : 'best available');
  }
  return { ok: true, engine: best.id, reason: why.join(', '), candidates: ranked.map((e) => e.id) };
}

/** Running average of observed durations (seconds), newest weighted 30%. */
export function recordPerf(perf: PerfTable, engine: string, model: string, seconds: number): PerfTable {
  if (!(seconds > 0)) return perf;
  const prev = perf[engine]?.[model];
  const next = prev ? prev * 0.7 + seconds * 0.3 : seconds;
  return { ...perf, [engine]: { ...(perf[engine] ?? {}), [model]: Math.round(next * 100) / 100 } };
}
