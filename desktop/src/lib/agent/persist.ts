// Pure (de)serialisation of per-session agent state so a bad or old save never breaks startup.
import { compactRun } from './reduce';
import type { AgentMessage, AgentRun, PermissionMode } from './types';

export interface AgentSessionState {
  project: string | null;
  permission: PermissionMode;
  runs: AgentRun[];
  transcript: AgentMessage[]; // OpenAI-style transcript, continued across tasks in this session
}

export const emptyAgentState = (): AgentSessionState => ({ project: null, permission: 'workspace_auto', runs: [], transcript: [] });

const PERMS: PermissionMode[] = ['ask', 'workspace_auto', 'full_access'];

export function hydrateAgent(raw: unknown): Record<string, AgentSessionState> {
  const out: Record<string, AgentSessionState> = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [id, v] of Object.entries(raw as Record<string, any>)) {
    if (!v || typeof v !== 'object') continue;
    const runs: AgentRun[] = (Array.isArray(v.runs) ? v.runs : []).filter((r: any) => r && typeof r.id === 'string' && Array.isArray(r.items));
    for (const r of runs) {
      // The app was closed or crashed mid-task: the work is resumable, never silently "running".
      if (r.status === 'running') {
        r.status = 'blocked';
        r.reason = 'Sarness was closed while this task was running. Use Continue task to resume.';
        for (const it of r.items) if (it.kind === 'tool' && (it.status === 'running' || it.status === 'awaiting')) {
          it.status = 'cancelled';
          it.content = it.content ?? 'Interrupted when Sarness closed.';
          if (it.approval?.state === 'waiting') it.approval.state = 'denied';
        }
      }
    }
    let permission: PermissionMode = PERMS.includes(v.permission) ? v.permission : 'workspace_auto';
    if (permission === 'full_access') permission = 'workspace_auto'; // Full Access is never restored silently
    out[id] = {
      project: typeof v.project === 'string' ? v.project : null,
      permission,
      runs,
      transcript: sanitizeTranscript(Array.isArray(v.transcript) ? v.transcript : []),
    };
  }
  return out;
}

/** Drops anything malformed and any tool message whose assistant tool_call is missing (API would reject it). */
export function sanitizeTranscript(t: any[]): AgentMessage[] {
  const out: AgentMessage[] = [];
  const open = new Set<string>();
  for (const m of t) {
    if (!m || typeof m.content !== 'string' && m.role !== 'assistant') continue;
    if (m.role === 'assistant') {
      out.push({ role: 'assistant', content: String(m.content ?? ''), ...(Array.isArray(m.tool_calls) && m.tool_calls.length ? { tool_calls: m.tool_calls } : {}) });
      open.clear();
      for (const c of m.tool_calls ?? []) open.add(c.id);
    } else if (m.role === 'tool') {
      if (open.has(m.tool_call_id)) out.push({ role: 'tool', tool_call_id: m.tool_call_id, content: m.content });
    } else if (m.role === 'user' || m.role === 'system') out.push({ role: m.role, content: m.content });
  }
  return out;
}

export function serializeAgent(all: Record<string, AgentSessionState>, keepFull = 2): string {
  const out: Record<string, AgentSessionState> = {};
  for (const [id, s] of Object.entries(all)) {
    if (!s.runs.length && !s.project && !s.transcript.length) continue;
    out[id] = { ...s, runs: s.runs.map((r, i) => compactRun(r, i >= s.runs.length - keepFull)), transcript: s.transcript.slice(-400) };
  }
  return JSON.stringify(out);
}
