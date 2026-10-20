// Experiments space: which experiment is open, which are mounted, and the engine's catalogue.
//
// Experiments run on one server at a time: by default the first that has them ready (primary first),
// or the one chosen in the Experiments screen. Their sessions, renders and documents live there.

import { apiFor } from '../api/client';
import { conn, engineList, onEvent } from '../stores/connection.svelte';
import { persistPrefs, prefs } from '../stores/prefs.svelte';
import { experimentById } from './registry';
import type { ExperimentDocument, ExperimentSummary } from './types';

export const exps = $state({
  /** Experiment on screen; null shows the selection screen. */
  active: null as string | null,
  /** Experiments that have been launched and stay mounted (their unsaved state survives switching away). */
  launched: [] as string[],
  catalogue: [] as ExperimentSummary[],
  loaded: false,
  error: null as string | null,
  /** A saved document an experiment should open next (set from the sidebar). */
  openRequest: null as { experiment: string; documentId: string } | null,
  /** Saved documents per experiment (summaries, newest first). */
  documents: {} as Record<string, ExperimentDocument[]>,
  /** Server running experiments (profile id). */
  engine: null as string | null,
  /** Catalogue of every online server, to choose where experiments run. */
  byEngine: {} as Record<string, ExperimentSummary[]>,
});

/** The API of the server running experiments. */
export const xapi = () => apiFor(exps.engine);
/** Whether an engine event came from the server running experiments. */
export const fromExperimentsEngine = (e: { engine?: string }) => !e.engine || e.engine === exps.engine;

const readyCount = (list: ExperimentSummary[] | undefined) => (list ?? []).filter((x) => x.availability.state === 'ready').length;

export async function loadCatalogue(): Promise<void> {
  const ids = engineList().filter((e) => e.state === 'online').map((e) => e.id);
  const results = await Promise.allSettled(ids.map((id) => apiFor(id).get<ExperimentSummary[]>('/v1/experiments')));
  let error: string | null = null;
  exps.byEngine = {};
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') exps.byEngine[ids[i]] = r.value;
    else error ??= r.reason instanceof Error ? r.reason.message : String(r.reason);
  });
  // Keep the current server while it is connected; else the chosen one; else where experiments are ready.
  const has = (id: string | null | undefined): id is string => !!id && !!exps.byEngine[id];
  const ready = ids.filter(has).sort((a, b) => readyCount(exps.byEngine[b]) - readyCount(exps.byEngine[a]))[0];
  const next = [exps.launched.length ? exps.engine : null, prefs.experimentsEngine, ready, conn.activeId].find(has) ?? null;
  if (next !== exps.engine && exps.engine) exps.documents = {};
  exps.engine = next;
  exps.catalogue = next ? exps.byEngine[next] : [];
  exps.error = next ? null : error;
  exps.loaded = true;
}

/** Runs experiments on another server. Open experiments close (unsaved drafts are kept locally). */
export function setExperimentsEngine(id: string): void {
  if (id === exps.engine || !exps.byEngine[id]) return;
  exps.launched = [];
  exps.active = null;
  exps.documents = {};
  exps.engine = id;
  exps.catalogue = exps.byEngine[id];
  prefs.experimentsEngine = id;
  persistPrefs();
}

export async function loadDocuments(experiment: string): Promise<void> {
  try {
    exps.documents[experiment] = await xapi().get<ExperimentDocument[]>(`/v1/experiments/${experiment}/documents`);
  } catch {
    /* the engine may predate experiments; the selection screen explains that */
  }
}

/** The Experiments tab: always lands on the selection screen, never straight into an experiment. */
export function enterExperiments(): void {
  exps.active = null;
  prefs.view = 'experiments';
  persistPrefs();
}

/** The Work tab: back to whatever Work view was open. */
export function enterWork(): void {
  prefs.view = prefs.workView ?? 'workspace';
  persistPrefs();
}

export function launch(id: string): void {
  if (!experimentById(id)) return;
  if (!exps.launched.includes(id)) exps.launched.push(id);
  exps.active = id;
  if (prefs.view !== 'experiments') {
    prefs.view = 'experiments';
    persistPrefs();
  }
}

export function openDocument(experiment: string, documentId: string): void {
  exps.openRequest = { experiment, documentId };
  launch(experiment);
}

/** Unmounts an experiment (its unsaved in-memory state is dropped; drafts kept by the experiment survive). */
export function closeExperiment(id: string): void {
  exps.launched = exps.launched.filter((x) => x !== id);
  if (exps.active === id) exps.active = null;
}

export function summaryFor(id: string): ExperimentSummary | undefined {
  return exps.catalogue.find((e) => e.id === id);
}

let wired = false;
export function wireExperimentEvents(): void {
  if (wired) return;
  wired = true;
  onEvent('experiment.documents', (e) => {
    const id = e.data?.experiment;
    if (id && exps.documents[id] && fromExperimentsEngine(e)) loadDocuments(id);
  });
  onEvent('experiment.install', (e) => {
    if (!e.data?.running) loadCatalogue();
  });
  // Servers coming and going can change where experiments are available.
  onEvent('connection.restored', () => loadCatalogue());
  onEvent('engine.disconnected', () => loadCatalogue());
}
