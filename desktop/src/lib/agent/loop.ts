// The desktop-side agent loop: send context to the model, validate and execute the tool
// calls it makes (locally, through injected `tool`), feed real results back, repeat until the
// model answers without tools, the user cancels, or the loop is genuinely blocked.
// All I/O is injected so the loop is fully testable with scripted models and tools.
import type { AgentMessage, ApprovalInfo, ChatToolCall, LoopEvent, ModelTurn, Phase, RunOutcome, ToolRequest, ToolResponse } from './types';

export interface LoopDeps {
  /** Streams one model turn. `onDelta` receives incremental text. */
  model(messages: AgentMessage[], signal: AbortSignal, onDelta: (content: string, reasoning: string) => void, onTool?: (name: string) => void): Promise<ModelTurn>;
  /** Executes a tool locally (native layer). Never throws for tool failures. */
  tool(req: Pick<ToolRequest, 'tool' | 'args' | 'call_id' | 'approval_id'>): Promise<ToolResponse>;
  /** Resolves true when the user approves, false when they deny (or the run is cancelled). */
  approve(info: ApprovalInfo, signal: AbortSignal): Promise<boolean>;
  emit(e: LoopEvent): void;
  sleep?(ms: number, signal: AbortSignal): Promise<void>;
}

export interface LoopOptions {
  maxSteps?: number;
  maxModelRetries?: number;
  /** Approximate character budget for the transcript sent to the model. */
  contextChars?: number;
}

export const MAX_TOOL_RESULT_CHARS = 14_000;
const WRITE_TOOLS = new Set(['write_file', 'edit_file', 'delete_path', 'move_path']);
const READ_TOOLS = new Set(['read_file', 'list_dir', 'search', 'find_files', 'git_status', 'git_diff', 'git_log', 'diagnostics', 'process_output', 'list_processes']);
const TEST_RE = /(^|[\s/_.-])(test|tests|spec|pytest|jest|vitest|mocha|ava|tap|check|lint|tsc|clippy|build|compile|verify)(?![a-z])/i;

export function phaseFor(name: string, args: Record<string, unknown>): Phase {
  if (WRITE_TOOLS.has(name)) return 'Editing';
  if (READ_TOOLS.has(name)) return 'Reading';
  if (name === 'run_command' || name === 'start_process') return TEST_RE.test(String(args.command ?? '')) ? 'Testing' : 'Running';
  if (name === 'git_commit' || name === 'git_branch') return 'Running';
  return 'Thinking';
}

export const sleepAbortable = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
  });

/** Old tool output is replaced by a short note when the transcript outgrows the budget. */
export function trimTranscript(messages: AgentMessage[], budget: number): AgentMessage[] {
  const size = (ms: AgentMessage[]) => ms.reduce((n, m) => n + m.content.length + (m.role === 'assistant' && m.tool_calls ? JSON.stringify(m.tool_calls).length : 0), 0);
  if (size(messages) <= budget) return messages;
  const out = messages.map((m) => ({ ...m })) as AgentMessage[];
  // Protect the system prompt and the most recent exchanges.
  const protectedFrom = Math.max(1, out.length - 8);
  for (let i = 1; i < protectedFrom && size(out) > budget; i++) {
    const m = out[i];
    if (m.role === 'tool' && m.content.length > 300) m.content = `[earlier tool output omitted to save context: ${m.content.length} characters]`;
    else if (m.role === 'assistant' && m.content.length > 1500) m.content = `${m.content.slice(0, 1200)}\n[…trimmed…]`;
  }
  // Still too large: drop the oldest whole exchanges (keeping tool_call/tool pairs consistent).
  let start = 1;
  while (size(out) > budget && start < protectedFrom) {
    let end = start + 1;
    const m = out[start];
    if (m.role === 'assistant' && m.tool_calls) while (end < out.length && out[end].role === 'tool') end++;
    if (m.role === 'user' && start === 1) { start = end; continue; } // keep the original task
    out.splice(start, end - start);
    start = Math.min(start, out.length);
    if (out.length - 1 <= 8) break;
  }
  return out;
}

const TEXT_TOOL_RE = /("name"\s*:\s*"(read_file|write_file|edit_file|run_command|list_dir|search)"|<tool_call>|"tool_calls?"\s*:|```(?:json|bash|sh)?\s*\n\s*\{\s*"(?:name|tool|command)")/;
export const looksLikeTextToolCall = (s: string) => TEXT_TOOL_RE.test(s);

const isTransient = (e: unknown) => {
  const x = e as { status?: number; message?: string; name?: string };
  if (x?.name === 'AbortError') return false;
  if (typeof x?.status === 'number') return x.status === 0 || x.status === 502 || x.status === 503 || x.status === 504 || x.status === 429 || x.status === 409;
  return /interrupted|network|fetch|connection|ECONN|timeout/i.test(x?.message ?? '');
};
const isContextOverflow = (e: unknown) => /context|too long|exceed|n_ctx|maximum/i.test((e as Error)?.message ?? '');

function clip(s: string): string {
  if (s.length <= MAX_TOOL_RESULT_CHARS) return s;
  const head = Math.floor(MAX_TOOL_RESULT_CHARS * 0.4);
  return `${s.slice(0, head)}\n…[${s.length - MAX_TOOL_RESULT_CHARS} characters omitted]…\n${s.slice(s.length - (MAX_TOOL_RESULT_CHARS - head))}`;
}

export function parseArgs(raw: string): { ok: true; args: Record<string, unknown> } | { ok: false; error: string } {
  const t = (raw ?? '').trim();
  if (!t) return { ok: true, args: {} };
  try {
    const v = JSON.parse(t);
    if (v && typeof v === 'object' && !Array.isArray(v)) return { ok: true, args: v as Record<string, unknown> };
    return { ok: false, error: 'arguments must be a JSON object' };
  } catch (e) {
    return { ok: false, error: `arguments were not valid JSON (${(e as Error).message})` };
  }
}

/**
 * Runs the loop. `messages` is the transcript (system prompt first); it is mutated in place so the
 * caller can persist it and continue after a stop or restart.
 */
export async function runLoop(messages: AgentMessage[], deps: LoopDeps, signal: AbortSignal, opts: LoopOptions = {}): Promise<RunOutcome> {
  const maxSteps = opts.maxSteps ?? 60;
  const retries = opts.maxModelRetries ?? 4;
  let budget = opts.contextChars ?? 90_000;
  const sleep = deps.sleep ?? sleepAbortable;
  let step = 0;
  let nudgedText = false;
  let nudgedVerify = false;
  let nudgedLength = false;
  let nudgedEmpty = false;
  let nudgedFailing = false;
  let checkFailing = false; // the most recent build/test command exited non-zero
  const recent: string[] = []; // signatures of recent tool calls, to catch an agent going in circles
  const failStreak = new Map<string, number>(); // consecutive failures per identical call
  let unverifiedEdits = false; // files changed with no later successful command
  let turnSeq = 0;

  const cancelled = (): RunOutcome => ({ status: 'cancelled' });

  while (true) {
    if (signal.aborted) return cancelled();
    if (step >= maxSteps) return { status: 'blocked', reason: `Reached the limit of ${maxSteps} steps. Use Continue task to keep going.` };
    step++;

    // ---- model turn, with retry on transient failures
    deps.emit({ type: 'phase', phase: 'Thinking' });
    const turnId = `t${Date.now().toString(36)}${turnSeq++}`;
    let turn: ModelTurn | null = null;
    let attempt = 0;
    let shrunk = false;
    while (!turn) {
      try {
        turn = await deps.model(trimTranscript(messages, budget), signal, (content, reasoning) => deps.emit({ type: 'text', id: turnId, text: content, reasoning }), (name) => deps.emit({ type: 'drafting', name }));
      } catch (e) {
        if (signal.aborted) return cancelled();
        if (isContextOverflow(e) && !shrunk) {
          shrunk = true;
          budget = Math.floor(budget / 2);
          deps.emit({ type: 'notice', level: 'info', text: 'The conversation outgrew the model’s context; older tool output was trimmed.' });
          continue;
        }
        if (isTransient(e) && attempt < retries) {
          attempt++;
          const wait = Math.min(15_000, 1000 * 2 ** (attempt - 1));
          deps.emit({ type: 'notice', level: 'warn', text: `Model connection problem (${(e as Error).message}). Retrying in ${Math.round(wait / 1000)}s (${attempt}/${retries})…` });
          await sleep(wait, signal);
          if (signal.aborted) return cancelled();
          continue;
        }
        return { status: 'error', reason: (e as Error).message || 'The model request failed.' };
      }
    }
    if (signal.aborted) return cancelled();

    const assistant: AgentMessage = { role: 'assistant', content: turn.content };
    if (turn.toolCalls.length) assistant.tool_calls = turn.toolCalls;
    messages.push(assistant);

    deps.emit({ type: 'drafting', name: null });
    // ---- cut off by the length limit with no tool call: ask for a shorter, step-wise reply once
    if (turn.finish === 'length' && !turn.toolCalls.length && !nudgedLength) {
      nudgedLength = true;
      deps.emit({ type: 'notice', level: 'warn', text: 'The model’s reply hit the length limit and was cut off. Asking it to continue in smaller steps.' });
      messages.push({ role: 'user', content: 'Your reply was cut off. Continue with smaller steps: make one focused tool call at a time and keep file contents short.' });
      continue;
    }
    // ---- an empty reply is never "done" (e.g. the model spent its whole output budget thinking)
    if (!turn.toolCalls.length && !turn.content.trim()) {
      if (!nudgedEmpty) {
        nudgedEmpty = true;
        deps.emit({ type: 'notice', level: 'warn', text: turn.finish === 'length' ? 'The model used its whole output budget without answering. Asking it to continue briefly.' : 'The model returned an empty reply. Asking it to continue.' });
        messages.push({ role: 'user', content: 'Your last reply was empty. Keep working with tool calls (keep your reasoning short), or if you are finished, give your final report: what changed, how you verified it, what is unmet.' });
        continue;
      }
      return { status: 'blocked', reason: 'The model returned an empty reply twice (it may be out of output budget or not suited to tool use). Try Continue task, or choose another model.' };
    }
    // ---- no tool calls: finished, unless the model needs a nudge
    if (!turn.toolCalls.length) {
      if (looksLikeTextToolCall(turn.content) && !nudgedText) {
        nudgedText = true;
        deps.emit({ type: 'notice', level: 'warn', text: 'The model wrote a tool call as plain text instead of using tool calling. Asking it to use the tool interface. If this repeats, choose a model that supports tool use.' });
        messages.push({ role: 'user', content: 'You wrote a tool call as text. Nothing was executed. Use the provided function-calling interface to call tools.' });
        continue;
      }
      if (checkFailing && !nudgedFailing) {
        nudgedFailing = true;
        deps.emit({ type: 'notice', level: 'warn', text: 'The agent wants to finish but the last build/test command failed; asking it to fix this or say so plainly.' });
        messages.push({ role: 'user', content: 'The last build or test command you ran FAILED (non-zero exit code) and you have not fixed it. Do not claim success. Either fix the problem and re-run, or report plainly that it still fails and why.' });
        continue;
      }
      if (unverifiedEdits && !nudgedVerify) {
        nudgedVerify = true;
        deps.emit({ type: 'notice', level: 'info', text: 'Files changed but nothing was run afterwards; asking the agent to verify.' });
        messages.push({ role: 'user', content: 'You changed files but have not run any command since. Verify your work now (build, run tests, or start the app and check it), then report the real results. Check each requirement of the original request against what you built; say plainly which are met, unmet or untested. If verification is impossible here, say exactly why.' });
        continue;
      }
      deps.emit({ type: 'text', id: turnId, text: turn.content, reasoning: turn.reasoning, final: true });
      if (looksLikeTextToolCall(turn.content)) return { status: 'blocked', reason: 'This model does not appear to support tool calling; it keeps writing tool calls as text.' };
      return { status: 'completed', final: turn.content };
    }

    // ---- execute each tool call, in order
    const answered = new Set<string>();
    for (const call of turn.toolCalls) {
      if (signal.aborted) break;
      const parsed = parseArgs(call.function.arguments);
      const name = call.function.name;
      const args = parsed.ok ? parsed.args : {};
      deps.emit({ type: 'tool_start', id: call.id, callId: call.id, name, args });
      deps.emit({ type: 'phase', phase: phaseFor(name, args) });
      if (!parsed.ok) {
        const msg = `Invalid arguments for ${name}: ${parsed.error}. Call the tool again with a valid JSON object.`;
        deps.emit({ type: 'tool_result', callId: call.id, status: 'failed', content: msg, ui: null });
        messages.push({ role: 'tool', tool_call_id: call.id, content: msg });
        answered.add(call.id);
        continue;
      }
      const sig = `${name}:${JSON.stringify(args)}`;
      recent.push(sig);
      if (recent.length > 12) recent.shift();
      // Going in circles = the same call failing again and again, or the same call a great many times.
      // (Re-running a passing test to double-check is fine.)
      const same = recent.filter((x) => x === sig).length;
      if ((failStreak.get(sig) ?? 0) >= 3 || same >= 7) {
        const msg = `The agent repeated the same ${name} call ${same} times without making progress.`;
        deps.emit({ type: 'tool_result', callId: call.id, status: 'denied', content: msg, ui: null });
        messages.push({ role: 'tool', tool_call_id: call.id, content: msg });
        for (const c of turn.toolCalls as ChatToolCall[]) if (!answered.has(c.id) && c.id !== call.id) messages.push({ role: 'tool', tool_call_id: c.id, content: 'Skipped.' });
        return { status: 'blocked', reason: `${msg} Use Continue task with new guidance, or stop and rephrase the request.` };
      }
      let res = await deps.tool({ tool: name, args, call_id: call.id });
      if (res.status === 'needs_approval') {
        const info: ApprovalInfo = { approvalId: res.approval_id, risk: res.risk, title: res.title, reason: res.reason, preview: res.preview, state: 'waiting' };
        deps.emit({ type: 'phase', phase: 'Waiting for approval' });
        deps.emit({ type: 'tool_approval', callId: call.id, approval: info });
        const ok = await deps.approve(info, signal);
        if (signal.aborted) break;
        deps.emit({ type: 'tool_approval_result', callId: call.id, approved: ok });
        deps.emit({ type: 'phase', phase: phaseFor(name, args) });
        res = ok ? await deps.tool({ tool: name, args, call_id: call.id, approval_id: res.approval_id }) : { status: 'denied', content: 'The user declined this action. Do not retry it unchanged; choose another approach or explain what you need.' };
      }
      if (res.status === 'ok' || res.status === 'error') {
        const failed = res.status === 'error' || (res.ui?.kind === 'command' && (res.ui.exit_code !== 0 || res.ui.timed_out) && !res.ui.cancelled);
        failStreak.set(sig, failed ? (failStreak.get(sig) ?? 0) + 1 : 0);
        deps.emit({ type: 'tool_result', callId: call.id, status: res.ui?.cancelled ? 'cancelled' : failed ? 'failed' : 'done', content: res.content, ui: res.ui });
        if (res.status === 'ok' && WRITE_TOOLS.has(name)) unverifiedEdits = true;
        if (res.status === 'ok' && (name === 'run_command' || name === 'start_process') && res.ui?.exit_code === 0) unverifiedEdits = false;
        if (name === 'run_command' && res.ui?.kind === 'command' && !res.ui.cancelled && TEST_RE.test(String(res.ui.command ?? ''))) {
          checkFailing = res.ui.exit_code !== 0 || !!res.ui.timed_out;
          if (checkFailing) nudgedFailing = false; // a new failure earns a new reminder
        }
        if (name === 'update_plan' && res.ui?.steps) deps.emit({ type: 'plan', steps: res.ui.steps });
        messages.push({ role: 'tool', tool_call_id: call.id, content: clip(res.content) });
      } else if (res.status === 'denied') {
        deps.emit({ type: 'tool_result', callId: call.id, status: 'denied', content: res.content, ui: null });
        messages.push({ role: 'tool', tool_call_id: call.id, content: res.content });
      } else {
        // approval requested again (fingerprint mismatch): treat as a refusal rather than loop forever
        const msg = 'The action could not be approved. Try a different approach.';
        deps.emit({ type: 'tool_result', callId: call.id, status: 'denied', content: msg, ui: null });
        messages.push({ role: 'tool', tool_call_id: call.id, content: msg });
      }
      answered.add(call.id);
    }
    if (signal.aborted) {
      // Keep the transcript valid for "Continue task": every tool_call needs a tool message.
      for (const c of turn.toolCalls as ChatToolCall[]) {
        if (!answered.has(c.id)) {
          messages.push({ role: 'tool', tool_call_id: c.id, content: 'Cancelled by the user before this ran.' });
          deps.emit({ type: 'tool_result', callId: c.id, status: 'cancelled', content: 'Cancelled by the user before this ran.', ui: null });
        }
      }
      return cancelled();
    }
  }
}
