import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

function fakeStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    key: (i: number) => [...m.keys()][i] ?? null,
    get length() { return m.size; },
    clear: () => m.clear(),
    _m: m,
  };
}

let storage: ReturnType<typeof fakeStorage>;
async function boot() {
  vi.resetModules();
  const sessions = await import('./sessions.svelte');
  const panes = await import('./panes.svelte');
  return { sessions, panes };
}
async function settle() {
  flushSync();
  await vi.advanceTimersByTimeAsync(500);
}

beforeEach(() => {
  vi.useFakeTimers();
  storage = fakeStorage();
  vi.stubGlobal('localStorage', storage);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('one conversation by default', () => {
  it('starts with exactly one pane showing one session', async () => {
    const { panes } = await boot();
    expect(panes.ws.panes).toHaveLength(1);
    expect(panes.visiblePanes()).toHaveLength(1);
    expect(panes.isMulti()).toBe(false);
  });

  it('saves the pane→conversation link on first launch, so a restart restores it', async () => {
    const first = await boot();
    const sid = first.panes.ws.panes[0].sessionId;
    const s = first.sessions.getSession(sid)!;
    s.draft = 'hello';
    s.mode = 'image';
    s.title = 'My talk';
    await settle();
    expect(storage._m.has('sarness.workspace.v2')).toBe(true);

    const second = await boot(); // simulated restart: fresh modules, same storage
    expect(second.panes.ws.panes[0].sessionId).toBe(sid);
    const restored = second.sessions.getSession(sid)!;
    expect(restored.mode).toBe('image');
    expect(restored.draft).toBe('hello');
    expect(restored.title).toBe('My talk');
    expect(second.sessions.store.list.filter((x) => x.id === sid)).toHaveLength(1);
  });

  it('does not accumulate blank conversations across restarts', async () => {
    for (let i = 0; i < 4; i++) {
      await boot();
      await settle();
    }
    const last = await boot();
    expect(last.sessions.store.list.length).toBeLessThanOrEqual(1);
  });

  it('survives a corrupt workspace save', async () => {
    storage.setItem('sarness.workspace.v2', '{not json');
    storage.setItem('sarness.sessions.v1', 'also bad');
    const { panes } = await boot();
    expect(panes.ws.panes).toHaveLength(1);
  });
});

describe('optional advanced layouts', () => {
  it('offers 1, 2, 3 and 4 side-by-side conversations, each with its own session', async () => {
    const { panes, sessions } = await boot();
    for (const n of [2, 3, 4] as const) {
      panes.setLayout(n);
      expect(panes.visiblePanes()).toHaveLength(n);
    }
    const ids = panes.ws.panes.map((p) => p.sessionId);
    expect(new Set(ids).size).toBe(4);
    for (const p of panes.ws.panes) sessions.getSession(p.sessionId)!.draft = 'work in progress';
    panes.setLayout(1);
    expect(panes.visiblePanes()).toHaveLength(1);
    expect(panes.ws.panes).toHaveLength(4); // panes with content are minimised, never destroyed
    expect(panes.ws.minimized).toHaveLength(3);
  });

  it('closes blank extra panes when returning to one conversation (no clutter in the default view)', async () => {
    const { panes } = await boot();
    panes.setLayout(3);
    panes.setLayout(1);
    expect(panes.ws.panes).toHaveLength(1);
    expect(panes.ws.minimized).toHaveLength(0);
  });

  it('never exceeds four panes and cannot close the last one', async () => {
    const { panes } = await boot();
    panes.setLayout(4);
    expect(panes.addPane()).toBeNull();
    panes.setLayout(1);
    for (const p of [...panes.ws.panes]) panes.closePane(p.id);
    expect(panes.ws.panes.length).toBeGreaterThanOrEqual(1);
  });

  it('maximise hides the others without unmounting them, restore brings them back', async () => {
    const { panes } = await boot();
    panes.setLayout(2);
    const [a] = panes.ws.panes;
    panes.toggleMaximize(a.id);
    expect(panes.visiblePanes().map((p) => p.id)).toEqual([a.id]);
    expect(panes.ws.panes).toHaveLength(2);
    panes.toggleMaximize(a.id);
    expect(panes.visiblePanes()).toHaveLength(2);
  });

  it('opening a history item focuses it if already open, else loads it in the focused pane', async () => {
    const { panes, sessions } = await boot();
    panes.setLayout(2);
    const other = sessions.newSession();
    other.draft = 'x'; // not empty, so it counts as history
    panes.openSession(other.id);
    expect(panes.focusedPane().sessionId).toBe(other.id);
    const second = panes.ws.panes[1];
    panes.openSession(second.sessionId);
    expect(panes.ws.focus).toBe(second.id);
  });

  it('keeps layout and ratios across restart', async () => {
    const a = await boot();
    a.panes.setLayout(3, 'rows');
    a.panes.ws.colRatio = 0.35;
    await settle();
    const b = await boot();
    expect(b.panes.visiblePanes()).toHaveLength(3);
    expect(b.panes.ws.orientation).toBe('rows');
    expect(b.panes.ws.colRatio).toBeCloseTo(0.35);
  });
});

describe('legacy state cleanup', () => {
  it('drops blank minimised panes saved by an older build, keeping real ones', async () => {
    const a = await boot();
    a.panes.setLayout(3);
    const [p1, p2, p3] = a.panes.ws.panes;
    a.sessions.getSession(p1.sessionId)!.draft = 'keep me';
    a.panes.setLayout(1); // p2/p3 blank -> closed now; recreate the old shape by hand:
    await settle();
    storage.setItem('sarness.workspace.v2', JSON.stringify({ panes: [p1, p2, p3], minimized: [p2.id, p3.id], maximized: null, focus: p1.id, orientation: 'cols', colRatio: 0.5, rowRatio: 0.5 }));
    const b = await boot();
    expect(b.panes.ws.panes.map((p) => p.id)).toEqual([p1.id]);
    expect(b.panes.ws.minimized).toEqual([]);
  });
});

describe('new conversation', () => {
  it('reuses a blank conversation instead of piling up empty ones', async () => {
    const { panes } = await boot();
    const first = panes.focusedPane().sessionId;
    expect(panes.startNewConversation()).toBe(first);
  });
  it('creates a fresh one when the current conversation has content', async () => {
    const { panes, sessions } = await boot();
    const cur = sessions.getSession(panes.focusedPane().sessionId)!;
    cur.draft = 'something';
    const id = panes.startNewConversation();
    expect(id).not.toBe(cur.id);
    expect(panes.focusedPane().sessionId).toBe(id);
    expect(sessions.getSession(cur.id)).toBeDefined(); // the old one stays in history
  });
});
