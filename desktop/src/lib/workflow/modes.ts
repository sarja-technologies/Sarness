// Simple ⇄ Advanced mode transitions. The engine is the single source of truth
// for compiling simple parameters to a graph and for deciding whether a graph
// can be represented in simple mode.
import { api, apiFor } from '../api/client';
import type { Workflow } from '../api/types';
import { confirmDialog } from '../ui/dialogs.svelte';

/** Returns the graph to show in Advanced mode. Existing advanced work is preserved. */
export async function switchToAdvanced(kind: 'image' | 'video', existing: Workflow | null, simple: Record<string, unknown>, engine: string | null = null): Promise<Workflow> {
  if (existing && existing.nodes?.length) return existing;
  return apiFor(engine).post<Workflow>('/v1/workflows/compile', { kind, simple });
}

/**
 * Returns simple params derived from the graph, `null` to keep the current simple
 * settings, or `false` if the user cancelled.
 */
export async function switchToSimple(graph: Workflow | null): Promise<Record<string, unknown> | null | false> {
  if (!graph || !graph.nodes?.length) return null;
  const res = await api.post<{ representable: boolean; simple: Record<string, unknown> | null; reasons: string[] }>(
    '/v1/workflows/simplify', { workflow: stripUi(graph) });
  if (res.representable) return res.simple;
  const ok = await confirmDialog(
    'This workflow can’t be shown in simple mode',
    `It ${res.reasons.join('; ')}.\n\nYour advanced workflow is kept exactly as it is — switch back to Advanced at any time. Simple mode will use its own settings.`,
    { confirmLabel: 'Switch to simple' },
  );
  return ok ? null : false;
}

/** Removes editor-only nodes/fields before sending a graph to the engine. */
export function stripUi(g: Workflow): Workflow {
  return {
    ...g,
    nodes: g.nodes.filter((n) => !n.type.startsWith('ui.')),
    edges: g.edges.filter((e) => !e.source.startsWith('ui') && !e.target.startsWith('ui')),
  };
}
