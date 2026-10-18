// Optional advanced layout: up to four independent conversations side by side.
// The default is ONE conversation. Panes stay mounted while hidden (minimised
// or behind a maximised pane) so streams and running jobs are never lost.
import { newSession, getSession, store as sessionStore } from './sessions.svelte';
import { isEmpty } from './sessions';

export interface Pane {
  id: string;
  sessionId: string;
}
export type Orientation = 'cols' | 'rows';

interface WorkspaceState {
  panes: Pane[];
  minimized: string[];
  maximized: string | null;
  focus: string | null;
  orientation: Orientation;
  colRatio: number;
  rowRatio: number;
}

export const MAX_PANES = 4;
const KEY = 'sarness.workspace.v2';

let seq = 0;
const newId = () => `p${Date.now().toString(36)}${(seq++).toString(36)}`;

function fresh(): WorkspaceState {
  const p = { id: newId(), sessionId: newSession().id };
  return { panes: [p], minimized: [], maximized: null, focus: p.id, orientation: 'cols', colRatio: 0.5, rowRatio: 0.5 };
}

function load(): WorkspaceState {
  try {
    const s = JSON.parse(localStorage.getItem(KEY) ?? 'null') as WorkspaceState | null;
    if (s && Array.isArray(s.panes) && s.panes.length && s.panes.length <= MAX_PANES) {
      // Drop panes whose session vanished; make sure at least one pane survives.
      const panes = s.panes.filter((p) => p && typeof p.sessionId === 'string' && getSession(p.sessionId));
      if (!panes.length) return fresh();
      const ids = new Set(panes.map((p) => p.id));
      return {
        ...s, panes,
        orientation: s.orientation ?? 'cols', colRatio: s.colRatio ?? 0.5, rowRatio: s.rowRatio ?? 0.5,
        minimized: (s.minimized ?? []).filter((i) => ids.has(i)),
        maximized: s.maximized && ids.has(s.maximized) ? s.maximized : null,
        focus: s.focus && ids.has(s.focus) ? s.focus : panes[0].id,
      };
    }
  } catch {
    /* corrupt state: start fresh */
  }
  return fresh();
}

export const ws = $state<WorkspaceState>(load());

let timer: ReturnType<typeof setTimeout> | null = null;
function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify($state.snapshot(ws)));
  } catch {
    /* storage unavailable */
  }
}
export function persistWorkspace() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(save, 120);
}

// Save on any change (including the very first launch, which previously saved nothing until an action).
$effect.root(() => {
  $effect(() => {
    JSON.stringify($state.snapshot(ws));
    persistWorkspace();
  });
});

// Blank minimised panes (left by an older layout change) are clutter: close them at startup.
{
  const blankMin = ws.panes.filter((p) => ws.minimized.includes(p.id) && (!getSession(p.sessionId) || isEmpty(getSession(p.sessionId)!)));
  if (blankMin.length && blankMin.length < ws.panes.length) {
    const drop = new Set(blankMin.map((p) => p.id));
    ws.panes = ws.panes.filter((p) => !drop.has(p.id));
    ws.minimized = ws.minimized.filter((i) => !drop.has(i));
    if (!ws.panes.some((p) => p.id === ws.focus)) ws.focus = ws.panes[0].id;
  }
}

// Blank conversations that no pane shows are noise in the history: drop them at startup.
{
  const shown = new Set(ws.panes.map((p) => p.sessionId));
  sessionStore.list = sessionStore.list.filter((s) => shown.has(s.id) || !isEmpty(s));
}

export const isMulti = () => visiblePanes().length > 1;

export function visiblePanes(): Pane[] {
  if (ws.maximized) return ws.panes.filter((p) => p.id === ws.maximized);
  return ws.panes.filter((p) => !ws.minimized.includes(p.id));
}

export function focusedPane(): Pane {
  return ws.panes.find((p) => p.id === ws.focus) ?? ws.panes[0];
}

export function addPane(): string | null {
  if (ws.panes.length >= MAX_PANES) return null;
  const p = { id: newId(), sessionId: newSession().id };
  ws.panes.push(p);
  ws.maximized = null;
  ws.focus = p.id;
  persistWorkspace();
  return p.id;
}

export function closePane(id: string) {
  if (ws.panes.length <= 1) return;
  ws.panes = ws.panes.filter((p) => p.id !== id);
  ws.minimized = ws.minimized.filter((i) => i !== id);
  if (ws.maximized === id) ws.maximized = null;
  if (ws.focus === id) ws.focus = ws.panes[0]?.id ?? null;
  persistWorkspace();
}

export function toggleMinimize(id: string) {
  if (ws.minimized.includes(id)) ws.minimized = ws.minimized.filter((i) => i !== id);
  else if (ws.panes.length - ws.minimized.length > 1) ws.minimized = [...ws.minimized, id];
  if (ws.maximized === id) ws.maximized = null;
  persistWorkspace();
}

export function toggleMaximize(id: string) {
  ws.maximized = ws.maximized === id ? null : id;
  ws.minimized = ws.minimized.filter((i) => i !== id);
  ws.focus = id;
  persistWorkspace();
}

export function focusPane(id: string) {
  if (ws.focus !== id) {
    ws.focus = id;
    persistWorkspace();
  }
}

export function swapPanes(a: string, b: string) {
  if (a === b) return;
  const i = ws.panes.findIndex((p) => p.id === a);
  const j = ws.panes.findIndex((p) => p.id === b);
  if (i < 0 || j < 0) return;
  const next = [...ws.panes];
  [next[i], next[j]] = [next[j], next[i]];
  ws.panes = next;
  persistWorkspace();
}

/**
 * Layout presets. Extra panes that hold a conversation are minimised (never destroyed);
 * extra panes that are still blank are simply closed so the default view stays clean.
 */
export function setLayout(n: 1 | 2 | 3 | 4, orientation: Orientation = ws.orientation) {
  ws.maximized = null;
  ws.orientation = orientation;
  ws.minimized = [];
  while (ws.panes.length < n) ws.panes.push({ id: newId(), sessionId: newSession().id });
  const extras = ws.panes.slice(n);
  const blank = new Set(extras.filter((p) => {
    const s = getSession(p.sessionId);
    return !s || isEmpty(s);
  }).map((p) => p.id));
  ws.panes = ws.panes.filter((p) => !blank.has(p.id));
  ws.minimized = ws.panes.slice(n).map((p) => p.id);
  if (!ws.panes.some((p) => p.id === ws.focus)) ws.focus = ws.panes[0].id;
  persistWorkspace();
}

/** Open an existing conversation in the focused pane (or a new one when it is already open elsewhere). */
export function openSession(sessionId: string) {
  const open = ws.panes.find((p) => p.sessionId === sessionId);
  if (open) {
    ws.minimized = ws.minimized.filter((i) => i !== open.id);
    ws.focus = open.id;
    if (ws.maximized) ws.maximized = open.id;
  } else {
    focusedPane().sessionId = sessionId;
  }
  persistWorkspace();
}

/** Start a fresh conversation in the focused pane; the previous one stays in history. */
export function startNewConversation(): string {
  const cur = getSession(focusedPane().sessionId);
  if (cur && isEmpty(cur)) return cur.id; // already a blank conversation
  const s = newSession();
  focusedPane().sessionId = s.id;
  persistWorkspace();
  return s.id;
}

export function paneIndexOf(id: string) {
  return ws.panes.findIndex((p) => p.id === id);
}

export function setPaneSession(paneId: string, sessionId: string) {
  const p = ws.panes.find((x) => x.id === paneId);
  if (p) {
    p.sessionId = sessionId;
    persistWorkspace();
  }
}
