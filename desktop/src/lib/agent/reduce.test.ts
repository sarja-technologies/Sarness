import { describe, expect, it } from 'vitest';
import { appendLive, applyEvent, compactRun, currentPhase, newRun, summarize } from './reduce';

const run = () => newRun('r', 'task', 'm', 'workspace_auto', 1);
const start = (r: any, id: string, name: string, args: any = {}) => applyEvent(r, { type: 'tool_start', id, callId: id, name, args });
const done = (r: any, id: string, ui: any, status: any = 'done') => applyEvent(r, { type: 'tool_result', callId: id, status, content: 'c', ui });

describe('run reducer', () => {
  it('streams assistant text into one item and marks the final answer', () => {
    const r = run();
    applyEvent(r, { type: 'text', id: 't1', text: 'Hel', reasoning: '' });
    applyEvent(r, { type: 'text', id: 't1', text: 'Hello', reasoning: '' });
    applyEvent(r, { type: 'text', id: 't1', text: 'Hello', reasoning: '', final: true });
    expect(r.items).toHaveLength(1);
    expect(r.items[0]).toMatchObject({ kind: 'text', text: 'Hello', final: true });
  });
  it('tracks a tool through approval to result and shows live output', () => {
    const r = run();
    start(r, 'c1', 'run_command', { command: 'npm test' });
    appendLive(r, 'c1', 'stdout', 'line1\n');
    appendLive(r, 'c1', 'stderr', 'warn\n');
    expect((r.items[0] as any).live).toBe('line1\nwarn\n');
    expect(currentPhase(r)).toBe('Testing');
    applyEvent(r, { type: 'tool_approval', callId: 'c1', approval: { approvalId: 'a', risk: 'medium', title: 't', reason: 'r', preview: null, state: 'waiting' } });
    expect(currentPhase(r)).toBe('Waiting for approval');
    applyEvent(r, { type: 'tool_approval_result', callId: 'c1', approved: true });
    done(r, 'c1', { kind: 'command', command: 'npm test', exit_code: 0 });
    expect((r.items[0] as any).live).toBeUndefined();
    expect((r.items[0] as any).approval.state).toBe('approved');
    appendLive(r, 'c1', 'stdout', 'late');
    expect((r.items[0] as any).live).toBeUndefined();
  });
  it('summarises changed files, commands and verification', () => {
    const r = run();
    start(r, 'a', 'write_file'); done(r, 'a', { kind: 'diff', path: 'a.js', added: 5, removed: 0, created: true });
    start(r, 'b', 'edit_file'); done(r, 'b', { kind: 'diff', path: 'a.js', added: 1, removed: 1 });
    start(r, 'c', 'run_command'); done(r, 'c', { kind: 'command', command: 'npm test', exit_code: 1 }, 'failed');
    let s = summarize(r);
    expect(s.files).toEqual([{ path: 'a.js', added: 6, removed: 1, created: true, deleted: false }]);
    expect(s.verification).toBe('failed');
    start(r, 'd', 'run_command'); done(r, 'd', { kind: 'command', command: 'npm test', exit_code: 0 });
    s = summarize(r);
    expect(s.verification).toBe('passed');
    expect(s.commands.map((c) => c.ok)).toEqual([false, true]);
  });
  it('a passing check followed by more edits is stale, not verified', () => {
    const r = run();
    start(r, 'c', 'run_command'); done(r, 'c', { kind: 'command', command: 'npm test', exit_code: 0 });
    start(r, 'a', 'edit_file'); done(r, 'a', { kind: 'diff', path: 'a', added: 1, removed: 0 });
    expect(summarize(r).verification).toBe('stale');
  });
  it('reports no verification when nothing was run', () => {
    const r = run();
    start(r, 'a', 'write_file'); done(r, 'a', { kind: 'diff', path: 'a', added: 1, removed: 0, created: true });
    expect(summarize(r).verification).toBe('none');
  });
  it('compaction clips big outputs of old runs only', () => {
    const r = run();
    start(r, 'c', 'run_command'); done(r, 'c', { kind: 'command', command: 'x', stdout: 'x'.repeat(10000), exit_code: 0 });
    expect(((compactRun(r, false).items[0] as any).ui.stdout as string).length).toBeLessThan(4100);
    expect(compactRun(r, true)).toBe(r);
  });
  it('plans update in place', () => {
    const r = run();
    applyEvent(r, { type: 'plan', steps: [{ text: 'a', status: 'pending' }] });
    applyEvent(r, { type: 'plan', steps: [{ text: 'a', status: 'done' }] });
    expect(r.items).toHaveLength(1);
    expect((r.items[0] as any).steps[0].status).toBe('done');
  });
});
