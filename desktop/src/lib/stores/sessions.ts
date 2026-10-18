// Pure session model for the one-conversation interface. A session is one
// conversation timeline: chat/code turns live in an engine conversation
// (convId); generated images and videos are timeline entries that reference
// engine jobs. Everything here is UI-free so it can be unit-tested.
import type { Conversation, Workflow } from '../api/types';
import { imageDefaults, sanitizeMode, videoDefaults, type ImageSettings, type Mode, type VideoSettings } from '../workspace/params';

export interface GenEntry {
  id: string;
  mode: 'image' | 'video';
  jobId: string;
  prompt: string;
  created: number; // unix seconds, same clock as chat messages
  summary: string[];
}

export interface Session {
  id: string;
  title: string;
  titled: boolean; // true once the title was derived from content (or renamed)
  convId: string | null;
  mode: Mode;
  entries: GenEntry[];
  hasAgent: boolean; // a project was chosen or a coding task ran (state lives in the agent store)
  draft: string;
  image: ImageSettings;
  video: VideoSettings;
  chatModel: string | null;
  chatSystem: string; // user's system prompt for Chat mode (Code mode uses the coding prompt)
  graphs: { image: Workflow | null; video: Workflow | null }; // optional node-editor graphs
  created: number;
  updated: number;
}

export const DEFAULT_TITLE = 'New conversation';
let seq = 0;
export const newId = (p: string) => `${p}${Date.now().toString(36)}${(seq++).toString(36)}${Math.random().toString(36).slice(2, 5)}`;

export function createSessionObject(now = Date.now() / 1000): Session {
  return {
    id: newId('s'), title: DEFAULT_TITLE, titled: false, convId: null, mode: 'chat', entries: [], hasAgent: false, draft: '',
    image: imageDefaults(), video: videoDefaults(), chatModel: null, chatSystem: '', graphs: { image: null, video: null }, created: now, updated: now,
  };
}

/** Defensive load: tolerate missing/old fields so a bad save can never break startup. */
export function hydrate(raw: unknown): Session[] {
  if (!Array.isArray(raw)) return [];
  const out: Session[] = [];
  for (const r of raw) {
    if (!r || typeof r !== 'object' || typeof (r as any).id !== 'string') continue;
    const base = createSessionObject();
    const s = r as Partial<Session>;
    out.push({
      ...base, ...s,
      mode: sanitizeMode(s.mode),
      entries: Array.isArray(s.entries) ? s.entries.filter((e) => e && typeof e.jobId === 'string' && (e.mode === 'image' || e.mode === 'video')) : [],
      image: { ...base.image, ...(s.image ?? {}) },
      video: { ...base.video, ...(s.video ?? {}) },
      graphs: { ...base.graphs, ...(s.graphs ?? {}) },
    });
  }
  return out;
}

export const isEmpty = (s: Session) => !s.convId && !s.entries.length && !s.hasAgent && !s.draft.trim();

export function titleFromPrompt(text: string): string {
  const t = text.replace(/\s+/g, ' ').trim();
  return t.length > 48 ? `${t.slice(0, 47).trimEnd()}…` : t || DEFAULT_TITLE;
}

/** Engine conversations that no session points at become history entries (older chats are kept). */
export function missingFromServer(sessions: Session[], convs: Conversation[]): Session[] {
  const known = new Set(sessions.map((s) => s.convId).filter(Boolean));
  return convs
    .filter((c) => !known.has(c.id))
    .map((c) => ({
      ...createSessionObject(c.created_at ?? c.updated_at),
      title: c.title || DEFAULT_TITLE, titled: true, convId: c.id, updated: c.updated_at ?? c.created_at,
      mode: (c.system_prompt?.startsWith('You are Sarness Code') ? 'code' : 'chat') as Mode,
    }));
}

export type TimelineItem =
  | { kind: 'message'; key: string; at: number; id: string }
  | { kind: 'gen'; key: string; at: number; entry: GenEntry }
  | { kind: 'run'; key: string; at: number; id: string };

/** One chronological timeline from the active chat path, generation entries and coding-agent runs. */
export function buildTimeline(messages: { id: string; created_at: number }[], entries: GenEntry[], runs: { id: string; created: number }[] = []): TimelineItem[] {
  const items: (TimelineItem & { i: number })[] = [
    ...messages.map((m, i) => ({ kind: 'message' as const, key: `m${m.id}`, at: m.created_at, id: m.id, i })),
    ...entries.map((e, i) => ({ kind: 'gen' as const, key: `g${e.id}`, at: e.created, entry: e, i })),
    ...runs.map((r, i) => ({ kind: 'run' as const, key: `r${r.id}`, at: r.created, id: r.id, i })),
  ];
  const rank = { message: 0, gen: 1, run: 2 } as const;
  // Stable chronological order; chat path order breaks ties so a user turn stays before its reply.
  return items.sort((a, b) => a.at - b.at || (a.kind === b.kind ? a.i - b.i : rank[a.kind] - rank[b.kind])).map(({ i: _i, ...rest }) => rest as TimelineItem);
}

export function groupByRecency<T extends { updated: number }>(items: T[], now = Date.now() / 1000): { label: string; items: T[] }[] {
  const day = 86400;
  const startToday = Math.floor(now / day) * day;
  const buckets: [string, (t: number) => boolean][] = [
    ['Today', (t) => t >= startToday],
    ['Yesterday', (t) => t >= startToday - day],
    ['Previous 7 days', (t) => t >= startToday - 7 * day],
    ['Older', () => true],
  ];
  const sorted = [...items].sort((a, b) => b.updated - a.updated);
  const out: { label: string; items: T[] }[] = buckets.map(([label]) => ({ label, items: [] }));
  for (const it of sorted) out[buckets.findIndex(([, f]) => f(it.updated))].items.push(it);
  return out.filter((g) => g.items.length);
}

export function matchesQuery(s: Session, q: string, serverHits: Set<string>): boolean {
  const t = q.trim().toLowerCase();
  if (!t) return true;
  return s.title.toLowerCase().includes(t) || s.entries.some((e) => e.prompt.toLowerCase().includes(t)) || (!!s.convId && serverHits.has(s.convId));
}

/**
 * Plain-text note appended to the chat system prompt so the language model knows what was
 * generated earlier in the same conversation. It cannot see the pixels, and says so.
 */
export function mediaContext(entries: GenEntry[], limit = 8): string {
  if (!entries.length) return '';
  const recent = entries.slice(-limit);
  const lines = recent.map((e, i) => {
    const p = e.prompt.replace(/\s+/g, ' ').trim();
    return `${i + 1}. ${e.mode}: "${p.length > 160 ? `${p.slice(0, 159)}…` : p}"${e.summary.length ? ` (${e.summary.join(', ')})` : ''}`;
  });
  return `\n\nEarlier in this conversation the user generated media with Sarness. You cannot see the pixels, only the prompts and settings used:\n${lines.join('\n')}`;
}
