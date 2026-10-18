// Experiments space: which experiment is open, which are mounted, and the engine's catalogue.

import { api } from '../api/client';
import { onEvent } from '../stores/connection.svelte';
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
});

export async function loadCatalogue(): Promise<void> {
  try {
    exps.catalogue = await api.get<ExperimentSummary[]>('/v1/experiments');
    exps.error = null;
  } catch (e) {
    exps.catalogue = [];
    exps.error = e instanceof Error ? e.message : String(e);
  } finally {
    exps.loaded = true;
  }
}

export async function loadDocuments(experiment: string): Promise<void> {
  try {
    exps.documents[experiment] = await api.get<ExperimentDocument[]>(`/v1/experiments/${experiment}/documents`);
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
    if (id && exps.documents[id]) loadDocuments(id);
  });
  onEvent('experiment.install', (e) => {
    if (!e.data?.running) loadCatalogue();
  });
}
