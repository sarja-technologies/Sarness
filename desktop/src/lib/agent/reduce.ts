// Pure reducers turning loop events into the run timeline, and the run into a summary.
import type { AgentItem, AgentRun, LoopEvent, Phase } from './types';

const MAX_LIVE = 60_000;

export function newRun(id: string, prompt: string, model: string | null, permission: AgentRun['permission'], now = Date.now() / 1000): AgentRun {
  return { id, prompt, created: now, status: 'running', model, permission, items: [] };
}

function tool(run: AgentRun, callId: string): Extract<AgentItem, { kind: 'tool' }> | undefined {
  return run.items.find((i): i is Extract<AgentItem, { kind: 'tool' }> => i.kind === 'tool' && i.callId === callId);
}

/** Applies one loop event to a run (mutating, so it works on Svelte state proxies). */
export function applyEvent(run: AgentRun, e: LoopEvent, now = Date.now() / 1000): void {
  switch (e.type) {
    case 'text': {
      const existing = run.items.find((i) => i.kind === 'text' && i.id === e.id) as Extract<AgentItem, { kind: 'text' }> | undefined;
      if (existing) {
        existing.text = e.text;
        existing.reasoning = e.reasoning || existing.reasoning;
        if (e.final) existing.final = true;
      } else if (e.text || e.reasoning || e.final) run.items.push({ kind: 'text', id: e.id, text: e.text, reasoning: e.reasoning || undefined, final: e.final });
      break;
    }
    case 'tool_start': {
      run.drafting = undefined;
      // Drop an empty narration item left by a turn that only called tools.
      run.items.push({ kind: 'tool', id: `tool-${e.callId}`, callId: e.callId, name: e.name, args: e.args, status: 'running', startedAt: now });
      break;
    }
    case 'tool_approval': {
      const t = tool(run, e.callId);
      if (t) {
        t.status = 'awaiting';
        t.approval = e.approval;
      }
      break;
    }
    case 'tool_approval_result': {
      const t = tool(run, e.callId);
      if (t?.approval) t.approval.state = e.approved ? 'approved' : 'denied';
      if (t) t.status = 'running';
      break;
    }
    case 'tool_result': {
      const t = tool(run, e.callId);
      if (t) {
        t.status = e.status;
        t.content = e.content;
        t.ui = e.ui ?? undefined;
        t.endedAt = now;
        t.live = undefined;
      }
      break;
    }
    case 'plan': {
      const existing = run.items.find((i) => i.kind === 'plan') as Extract<AgentItem, { kind: 'plan' }> | undefined;
      if (existing) existing.steps = e.steps;
      else run.items.push({ kind: 'plan', id: `plan-${run.id}`, steps: e.steps });
      break;
    }
    case 'notice':
      run.items.push({ kind: 'notice', id: `n${run.items.length}-${Math.random().toString(36).slice(2, 6)}`, level: e.level, text: e.text });
      break;
    case 'phase':
      run.phase = e.phase;
      break;
    case 'drafting':
      run.drafting = e.name ?? undefined;
      break;
  }
}

/** Appends streamed command output to the running tool card. */
export function appendLive(run: AgentRun, callId: string, stream: 'stdout' | 'stderr', text: string): void {
  const t = tool(run, callId);
  if (!t || t.status === 'done' || t.status === 'failed') return;
  const next = (t.live ?? '') + text;
  t.live = next.length > MAX_LIVE ? `…${next.slice(next.length - MAX_LIVE)}` : next;
  void stream;
}

export interface RunSummary {
  files: { path: string; added: number; removed: number; created: boolean; deleted: boolean; moved?: string }[];
  commands: { command: string; exit: number | null; ok: boolean; test: boolean }[];
  verification: 'passed' | 'failed' | 'stale' | 'none'; // stale: files changed after the last passing check
  denied: number;
}

const TEST_RE = /(^|[\s/_.-])(test|tests|spec|pytest|jest|vitest|mocha|ava|tap|check|tsc|clippy|build|compile|lint|verify)(?![a-z])/i;

export function summarize(run: AgentRun): RunSummary {
  const files = new Map<string, RunSummary['files'][number]>();
  const commands: RunSummary['commands'] = [];
  let denied = 0;
  let lastEdit = -1;
  let lastTest = -1;
  let lastTestOk = false;
  run.items.forEach((it, idx) => {
    if (it.kind !== 'tool') return;
    if (it.status === 'denied') denied++;
    const ui = it.ui;
    if (!ui || it.status === 'cancelled') return;
    if (ui.kind === 'diff') {
      const f = files.get(ui.path) ?? { path: ui.path, added: 0, removed: 0, created: false, deleted: false };
      f.added += ui.added ?? 0;
      f.removed += ui.removed ?? 0;
      f.created = f.created || !!ui.created;
      f.deleted = false;
      files.set(ui.path, f);
      lastEdit = idx;
    } else if (ui.kind === 'delete') {
      const f = files.get(ui.path) ?? { path: ui.path, added: 0, removed: 0, created: false, deleted: false };
      f.deleted = true;
      files.set(ui.path, f);
      lastEdit = idx;
    } else if (ui.kind === 'move') {
      files.set(ui.to, { path: ui.to, added: 0, removed: 0, created: false, deleted: false, moved: ui.from });
      lastEdit = idx;
    } else if (ui.kind === 'command') {
      const ok = ui.exit_code === 0 && !ui.timed_out;
      const test = TEST_RE.test(String(ui.command));
      commands.push({ command: String(ui.command), exit: ui.exit_code ?? null, ok, test });
      if (test) {
        lastTest = idx;
        lastTestOk = ok;
      }
    }
  });
  const verification: RunSummary['verification'] = lastTest < 0 ? 'none' : !lastTestOk ? 'failed' : lastTest > lastEdit ? 'passed' : 'stale';
  return { files: [...files.values()], commands, verification, denied };
}

export function currentPhase(run: AgentRun): Phase {
  if (run.status === 'running' && run.phase) return run.phase;
  if (run.status === 'completed') return 'Completed';
  if (run.status === 'cancelled') return 'Stopped';
  if (run.status === 'blocked') return 'Blocked';
  if (run.status === 'error') return 'Failed';
  const last = [...run.items].reverse().find((i) => i.kind === 'tool' && i.status !== 'done' && i.status !== 'failed' && i.status !== 'denied' && i.status !== 'cancelled') as Extract<AgentItem, { kind: 'tool' }> | undefined;
  if (last) {
    if (last.status === 'awaiting') return 'Waiting for approval';
    if (['write_file', 'edit_file', 'delete_path', 'move_path'].includes(last.name)) return 'Editing';
    if (['read_file', 'list_dir', 'search', 'find_files', 'git_status', 'git_diff', 'git_log', 'diagnostics'].includes(last.name)) return 'Reading';
    if (last.name === 'run_command' || last.name === 'start_process') return TEST_RE.test(String(last.args.command ?? '')) ? 'Testing' : 'Running';
  }
  return 'Thinking';
}

/** Keeps persisted history small: long outputs of older runs are clipped. */
export function compactRun(run: AgentRun, full: boolean): AgentRun {
  if (full) return run;
  const clip = (s: unknown, n: number) => (typeof s === 'string' && s.length > n ? `${s.slice(0, n)}\n…[clipped]` : s);
  return {
    ...run,
    items: run.items.map((it) =>
      it.kind === 'tool'
        ? { ...it, live: undefined, content: clip(it.content, 2000) as string | undefined, ui: it.ui ? { ...it.ui, stdout: clip(it.ui.stdout, 4000), stderr: clip(it.ui.stderr, 2000), diff: clip(it.ui.diff, 6000) } : it.ui }
        : it,
    ),
  };
}
