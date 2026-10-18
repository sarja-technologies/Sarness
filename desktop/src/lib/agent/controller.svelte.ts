// Binds the pure loop to the real world for one session: the engine (inference only), the
// native layer (all execution) and reactive state for the UI.
import { ApiError, api, streamChat } from '../api/client';
import { models } from '../stores/models.svelte';
import { newId, titleFromPrompt } from '../stores/sessions';
import { runLoop, type LoopDeps } from './loop';
import { native } from './native';
import { systemPrompt, workspaceTitle } from './prompt';
import { appendLive, applyEvent, newRun } from './reduce';
import { diffSnapshots, mergeChanges, type Snapshot } from './snapshot';
import { agentFor } from './state.svelte';
import type { AgentMessage, AgentRun, ApprovalInfo, ModelTurn, PermissionMode, RunOutcome } from './types';

const MIN_ENGINE = [0, 2, 0];

export class EngineTooOld extends Error {
  constructor(version: string | null) {
    super(`The Sarness Engine on this server${version ? ` (v${version})` : ''} does not support tool calling yet. Update it to v0.2.0 or newer to use the coding agent. Nothing was changed.`);
  }
}

function olderThan(v: string, min: number[]): boolean {
  const p = v.split(/[.+-]/).map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < 3; i++) if ((p[i] ?? 0) !== min[i]) return (p[i] ?? 0) < min[i];
  return false;
}

let supportChecked: Promise<void> | null = null;
export function resetSupportCheck() {
  supportChecked = null;
}
async function ensureEngineSupport(): Promise<void> {
  supportChecked ??= (async () => {
    const h = await api.get<{ engine_version?: string }>('/v1/health');
    if (!h.engine_version || olderThan(h.engine_version, MIN_ENGINE)) throw new EngineTooOld(h.engine_version ?? null);
  })();
  try {
    await supportChecked;
  } catch (e) {
    supportChecked = null;
    throw e;
  }
}

let toolsCache: any[] | null = null;

export class AgentController {
  running = $state(false);
  error = $state<string | null>(null);
  private abort: AbortController | null = null;
  private waiters = new Map<string, (ok: boolean) => void>();
  private unlisten: (() => void) | null = null;
  readonly sessionId: string;
  private getModel: () => { ref: string | null; ctx: number | null; name?: string };
  private onTitle: (t: string) => void;

  constructor(sessionId: string, getModel: AgentController['getModel'], onTitle: (t: string) => void) {
    this.sessionId = sessionId;
    this.getModel = getModel;
    this.onTitle = onTitle;
  }

  get state() {
    return agentFor(this.sessionId);
  }
  get lastRun(): AgentRun | undefined {
    return this.state.runs.at(-1);
  }
  get canContinue() {
    const r = this.lastRun;
    return !this.running && !!r && (r.status === 'blocked' || r.status === 'cancelled' || r.status === 'error');
  }

  /** The user clicked Approve / Deny on a pending request. */
  resolveApproval(approvalId: string, ok: boolean) {
    this.waiters.get(approvalId)?.(ok);
  }

  stop() {
    this.abort?.abort();
    const r = this.lastRun;
    if (r) void native.cancel(r.id);
    for (const w of this.waiters.values()) w(false);
  }

  async submit(prompt: string): Promise<boolean> {
    const text = prompt.trim();
    if (!text || this.running) return false;
    const st = this.state;
    if (!st.project) {
      this.error = 'Choose a project folder first.';
      return false;
    }
    const { ref } = this.getModel();
    if (!ref) {
      this.error = 'Choose a model first.';
      return false;
    }
    if (!native.available) {
      this.error = 'The local coding agent runs in the Sarness desktop app.';
      return false;
    }
    this.error = null;
    try {
      await ensureEngineSupport();
    } catch (e) {
      this.error = (e as Error).message;
      return false;
    }
    st.transcript = this.withSystem(st.transcript);
    st.transcript.push({ role: 'user', content: text });
    const run = newRun(newId('run'), text, ref, st.permission);
    st.runs.push(run);
    this.onTitle(titleFromPrompt(text));
    void this.execute(this.lastRun!);
    return true;
  }

  /** Resumes after Stop, an error, the step limit, or an app restart. */
  async continueTask(): Promise<void> {
    const r = this.lastRun;
    if (!r || this.running) return;
    this.error = null;
    try {
      await ensureEngineSupport();
    } catch (e) {
      this.error = (e as Error).message;
      return;
    }
    const st = this.state;
    st.transcript = this.withSystem(st.transcript);
    const last = st.transcript.at(-1);
    if (!last || (last.role !== 'user' && last.role !== 'tool')) st.transcript.push({ role: 'user', content: 'Continue the task from where you stopped. Re-check the current state of the project before acting.' });
    await native.resetRun(r.id);
    r.status = 'running';
    r.reason = undefined;
    r.ended = undefined;
    r.permission = st.permission;
    void this.execute(r);
  }

  private withSystem(t: AgentMessage[]): AgentMessage[] {
    const sys: AgentMessage = {
      role: 'system',
      content: systemPrompt({ workspace: this.state.project!, os: navigator.platform?.toLowerCase().includes('linux') ? 'Linux' : navigator.platform || 'desktop', permission: this.state.permission, today: new Date().toISOString().slice(0, 10) }),
    };
    return t[0]?.role === 'system' ? [sys, ...t.slice(1)] : [sys, ...t];
  }

  private async execute(run: AgentRun) {
    const st = this.state;
    const project = st.project!;
    this.running = true;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    // Baseline of the project's files, to report what REALLY changed on disk afterwards.
    let baseline: { files: Snapshot; truncated: boolean } | null = null;
    try {
      baseline = await native.snapshot(project);
    } catch {
      /* summary falls back to tool-reported changes */
    }
    this.unlisten = await native.onOutput((c) => c.run_id === run.id && appendLive(run, c.call_id, c.stream, c.text)).catch(() => null) as (() => void) | null;
    const { ref, ctx } = this.getModel();
    const modelRef = run.model ?? ref!;

    const deps: LoopDeps = {
      model: (messages, sig, onDelta, onTool) => this.callModel(modelRef, messages, sig, onDelta, onTool),
      tool: async (req) => {
        try {
          return await native.call({ workspace: project, run_id: run.id, call_id: req.call_id, tool: req.tool, args: req.args, mode: st.permission, approval_id: req.approval_id ?? null });
        } catch (e) {
          return { status: 'error', content: `The local tool layer failed: ${(e as Error).message ?? e}`, ui: null };
        }
      },
      approve: (info: ApprovalInfo, sig: AbortSignal) =>
        new Promise<boolean>((resolve) => {
          const done = (ok: boolean) => {
            this.waiters.delete(info.approvalId);
            resolve(ok);
          };
          this.waiters.set(info.approvalId, done);
          sig.addEventListener('abort', () => done(false), { once: true });
          if (sig.aborted) done(false);
        }),
      emit: (e) => applyEvent(run, e),
    };
    let outcome: RunOutcome;
    try {
      const budget = Math.max(24_000, Math.floor((ctx ?? 32_768) * 3 * 0.55) - 12_000);
      outcome = await runLoop(st.transcript, deps, signal, { contextChars: budget });
    } catch (e) {
      outcome = { status: 'error', reason: (e as Error).message };
    }
    await this.recordDiskChanges(run, project, baseline);
    this.finish(run, outcome);
  }

  private async recordDiskChanges(run: AgentRun, project: string, baseline: { files: Snapshot; truncated: boolean } | null) {
    if (!baseline) return;
    try {
      const after = await native.snapshot(project);
      const d = diffSnapshots(baseline.files, after.files);
      d.truncated = baseline.truncated || after.truncated || undefined;
      run.disk = mergeChanges(run.disk, d);
    } catch {
      /* project folder vanished or tool layer unavailable */
    }
  }

  private finish(run: AgentRun, outcome: RunOutcome) {
    run.status = outcome.status === 'completed' ? 'completed' : outcome.status;
    run.reason = 'reason' in outcome ? outcome.reason : undefined;
    run.ended = Date.now() / 1000;
    run.phase = undefined;
    run.drafting = undefined;
    for (const it of run.items) if (it.kind === 'tool' && (it.status === 'running' || it.status === 'awaiting')) it.status = 'cancelled';
    this.unlisten?.();
    this.unlisten = null;
    this.abort = null;
    this.running = false;
    this.waiters.clear();
    // Background processes (dev servers) started by this run are stopped when it ends.
    void native.endRun(run.id).then((n) => {
      if (n > 0) applyEvent(run, { type: 'notice', level: 'info', text: `Stopped ${n} background process${n === 1 ? '' : 'es'} started by this task.` });
    });
  }

  private async callModel(modelRef: string, messages: AgentMessage[], signal: AbortSignal, onDelta: (c: string, r: string) => void, onTool?: (name: string) => void): Promise<ModelTurn> {
    toolsCache ??= await native.toolDefs();
    let content = '';
    let reasoning = '';
    let done: any = null;
    try {
      await streamChat(
        '/v1/agent/completions',
        { model_ref: modelRef, messages, tools: toolsCache, tool_choice: 'auto', params: { temperature: 0.2, max_tokens: 6144 } },
        (ev: any) => {
          if (ev.type === 'delta') {
            content += ev.content ?? '';
            reasoning += ev.reasoning ?? '';
            onDelta(content, reasoning);
          } else if (ev.type === 'tool_call') onTool?.(ev.name);
          else if (ev.type === 'done') done = ev;
          else if (ev.type === 'error') throw new Error(ev.message);
        },
        signal,
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 404 && /not found/i.test(e.message)) {
        resetSupportCheck();
        throw new EngineTooOld(null);
      }
      throw e;
    }
    if (!done) {
      if (signal.aborted) throw new DOMException('aborted', 'AbortError');
      throw new ApiError(0, 'Connection to the engine was interrupted while the model was replying.');
    }
    return { content: done.message?.content ?? content, reasoning: done.reasoning ?? reasoning, toolCalls: done.message?.tool_calls ?? [], finish: done.finish_reason ?? null };
  }
}

export { workspaceTitle };
export type { PermissionMode };
