import { describe, expect, it } from 'vitest';
import type { Conversation } from '../api/types';
import { mediaContext, buildTimeline, createSessionObject, groupByRecency, hydrate, matchesQuery, missingFromServer, titleFromPrompt, type GenEntry } from './sessions';

const entry = (id: string, created: number, prompt = 'p'): GenEntry => ({ id, mode: 'image', jobId: `j${id}`, prompt, created, summary: [] });
const conv = (id: string, extra: Partial<Conversation> = {}) => ({ id, title: `T${id}`, created_at: 100, updated_at: 200, system_prompt: '', ...extra }) as Conversation;

describe('timeline', () => {
  it('interleaves chat messages and generations chronologically in one list', () => {
    const t = buildTimeline([{ id: 'a', created_at: 10 }, { id: 'b', created_at: 30 }], [entry('x', 20), entry('y', 40)]);
    expect(t.map((i) => i.key)).toEqual(['ma', 'gx', 'mb', 'gy']);
  });
  it('keeps order stable for identical timestamps (message before generation)', () => {
    const t = buildTimeline([{ id: 'a', created_at: 5 }], [entry('x', 5)]);
    expect(t.map((i) => i.kind)).toEqual(['message', 'gen']);
  });
  it('shows a session with no chat at all', () => {
    expect(buildTimeline([], [entry('x', 1)])).toHaveLength(1);
  });
});

describe('history', () => {
  it('imports engine conversations that have no session, once', () => {
    const s = createSessionObject();
    s.convId = 'c1';
    const added = missingFromServer([s], [conv('c1'), conv('c2'), conv('c3', { system_prompt: 'You are Sarness Code, careful' })]);
    expect(added.map((a) => a.convId)).toEqual(['c2', 'c3']);
    expect(added[1].mode).toBe('code');
    expect(missingFromServer([s, ...added], [conv('c1'), conv('c2'), conv('c3')])).toHaveLength(0);
  });
  it('groups by recency', () => {
    const now = 1_000_000;
    const g = groupByRecency([{ updated: now - 10 }, { updated: now - 90_000 }, { updated: now - 5 * 86400 }, { updated: now - 40 * 86400 }], now);
    expect(g.map((x) => x.label)).toEqual(['Today', 'Yesterday', 'Previous 7 days', 'Older']);
  });
  it('searches titles, generation prompts and server message hits', () => {
    const s = createSessionObject();
    s.title = 'Lighthouse notes';
    s.entries = [entry('1', 1, 'a paper boat')];
    s.convId = 'c9';
    expect(matchesQuery(s, 'light', new Set())).toBe(true);
    expect(matchesQuery(s, 'PAPER', new Set())).toBe(true);
    expect(matchesQuery(s, 'zebra', new Set())).toBe(false);
    expect(matchesQuery(s, 'zebra', new Set(['c9']))).toBe(true);
  });
  it('derives short titles', () => {
    expect(titleFromPrompt('  hello   world ')).toBe('hello world');
    expect(titleFromPrompt('x'.repeat(100)).length).toBeLessThanOrEqual(48);
  });
});

describe('persistence', () => {
  it('survives corrupt or old saved data', () => {
    expect(hydrate(null)).toEqual([]);
    const [s] = hydrate([{ id: 's1', mode: 'bogus', entries: [{ nope: 1 }, { jobId: 'j', mode: 'video', id: 'e', prompt: '', created: 1, summary: [] }], image: { steps: 5 } }, { bad: true }]);
    expect(s.mode).toBe('chat');
    expect(s.entries).toHaveLength(1);
    expect(s.image.steps).toBe(5);
    expect(s.image.aspect).toBe('1:1'); // missing fields get defaults
    expect(s.video.frames).toBe(33);
  });
});

describe('media context for the language model', () => {
  it('is empty without generations', () => expect(mediaContext([])).toBe(''));
  it('lists prompts and states the model cannot see pixels', () => {
    const c = mediaContext([entry('1', 1, 'a red boat'), { ...entry('2', 2, 'x'.repeat(300)), mode: 'video' }]);
    expect(c).toContain('cannot see the pixels');
    expect(c).toContain('image: "a red boat"');
    expect(c).toContain('video: "');
    expect(c.length).toBeLessThan(600);
  });
  it('keeps only the most recent generations', () => {
    const many = Array.from({ length: 20 }, (_, i) => entry(String(i), i, `prompt-${i}`));
    const c = mediaContext(many, 3);
    expect(c).toContain('prompt-19');
    expect(c).not.toContain('prompt-10');
  });
});
