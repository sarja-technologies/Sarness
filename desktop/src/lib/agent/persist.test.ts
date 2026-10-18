import { describe, expect, it } from 'vitest';
import { hydrateAgent, sanitizeTranscript, serializeAgent } from './persist';

const run = (status: string, items: any[] = []): any => ({ id: 'r1', prompt: 'p', created: 1, status, model: 'm', permission: 'ask', items });

describe('agent persistence', () => {
  it('restores project, permission, runs and transcript', () => {
    const raw = { s1: { project: '/home/u/app', permission: 'ask', runs: [run('completed')], transcript: [{ role: 'system', content: 's' }, { role: 'user', content: 'u' }] } };
    const out = hydrateAgent(JSON.parse(JSON.stringify(raw)));
    expect(out.s1.project).toBe('/home/u/app');
    expect(out.s1.permission).toBe('ask');
    expect(out.s1.runs[0].status).toBe('completed');
    expect(out.s1.transcript).toHaveLength(2);
  });
  it('a run that was running when the app closed becomes resumable "blocked", with its open tools cancelled', () => {
    const items = [{ kind: 'tool', id: 't', callId: 'c', name: 'run_command', args: {}, status: 'running', startedAt: 1 }, { kind: 'tool', id: 't2', callId: 'c2', name: 'x', args: {}, status: 'awaiting', approval: { state: 'waiting' }, startedAt: 1 }];
    const r = hydrateAgent({ s: { runs: [run('running', items)] } }).s.runs[0];
    expect(r.status).toBe('blocked');
    expect(r.reason).toContain('Continue task');
    expect((r.items[0] as any).status).toBe('cancelled');
    expect((r.items[1] as any).approval.state).toBe('denied');
  });
  it('Full Access is never restored silently', () => {
    expect(hydrateAgent({ s: { permission: 'full_access' } }).s.permission).toBe('workspace_auto');
    expect(hydrateAgent({ s: { permission: 'bogus' } }).s.permission).toBe('workspace_auto');
  });
  it('tolerates garbage', () => {
    expect(hydrateAgent(null)).toEqual({});
    expect(hydrateAgent({ a: 5, b: { runs: 'x', transcript: 7 } }).b.runs).toEqual([]);
  });
  it('drops orphan tool messages so the model API never rejects the transcript', () => {
    const t = sanitizeTranscript([
      { role: 'user', content: 'u' },
      { role: 'tool', tool_call_id: 'zzz', content: 'orphan' },
      { role: 'assistant', content: '', tool_calls: [{ id: 'a', type: 'function', function: { name: 'x', arguments: '{}' } }] },
      { role: 'tool', tool_call_id: 'a', content: 'ok' },
    ]);
    expect(t.map((m) => m.role)).toEqual(['user', 'assistant', 'tool']);
  });
  it('serialises compactly, clipping old runs but not the latest', () => {
    const big = (id: string): any => ({ ...run('completed', [{ kind: 'tool', id, callId: id, name: 'run_command', args: {}, status: 'done', ui: { stdout: 'x'.repeat(20000) }, startedAt: 1 }]), id });
    const json = serializeAgent({ s: { project: '/p', permission: 'ask', runs: [big('a'), big('b'), big('c'), big('d')], transcript: [] } });
    const back = JSON.parse(json).s.runs;
    expect(back[0].items[0].ui.stdout.length).toBeLessThan(4100);
    expect(back[3].items[0].ui.stdout.length).toBe(20000);
  });
});
