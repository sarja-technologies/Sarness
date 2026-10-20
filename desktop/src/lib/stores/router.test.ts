import { describe, expect, it } from 'vitest';
import { recordPerf, route, type EngineView, type RoutingPolicy } from './router';

const eng = (id: string, o: Partial<EngineView> = {}): EngineView => ({
  id, name: id.toUpperCase(), online: true, preferredTasks: [], models: new Set(), loaded: new Set(), freeMb: null,
  jobsRunning: 0, jobsQueued: 0, queueSoftLimit: 8, llmStreams: 0, ...o,
});
const auto: RoutingPolicy = { modelPrefs: {}, fallback: 'any' };
const ok = (r: ReturnType<typeof route>) => (r.ok ? r.engine : `ERR ${r.error}`);

describe('route', () => {
  const rtx = eng('rtx', { primary: true, models: new Set(['flux', 'local:qwen3-8b']), freeMb: 11000 });
  const spark = eng('spark', { models: new Set(['flux', 'qwen-image-2.1', 'local:gpt-oss-120b']), freeMb: 110000 });

  it('sends a model to the only server that has it, never substituting another model', () => {
    expect(ok(route({ task: 'chat', model: 'local:gpt-oss-120b' }, [rtx, spark], auto))).toBe('spark');
    expect(ok(route({ task: 'chat', model: 'local:qwen3-8b' }, [rtx, spark], auto))).toBe('rtx');
    const r = route({ task: 'image', model: 'sdxl' }, [rtx, spark], auto);
    expect(r.ok).toBe(false);
    expect(!r.ok && r.error).toMatch(/No connected server has this model/);
  });

  it('honours a manual choice exactly and explains why it cannot be used', () => {
    expect(ok(route({ task: 'image', model: 'flux', manual: 'rtx' }, [rtx, spark], auto))).toBe('rtx');
    const missing = route({ task: 'image', model: 'qwen-image-2.1', manual: 'rtx' }, [rtx, spark], auto);
    expect(!missing.ok && missing.error).toMatch(/RTX does not have this model/);
    const off = route({ task: 'image', model: 'flux', manual: 'rtx' }, [{ ...rtx, online: false }, spark], auto);
    expect(!off.ok && off.error).toMatch(/RTX is offline/); // no silent move to the other server
  });

  it('skips offline servers and reports when every server with the model is offline', () => {
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, online: false }, spark], auto))).toBe('spark');
    const r = route({ task: 'chat', model: 'local:gpt-oss-120b' }, [rtx, { ...spark, online: false }], auto);
    expect(!r.ok && r.error).toMatch(/only installed on SPARK, which is offline/);
  });

  it('prefers the per-model server, then the per-task server', () => {
    expect(ok(route({ task: 'image', model: 'flux' }, [rtx, spark], { ...auto, modelPrefs: { flux: 'spark' } }))).toBe('spark');
    expect(ok(route({ task: 'image', model: 'flux' }, [rtx, { ...spark, preferredTasks: ['image'] }], auto))).toBe('spark');
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, preferredTasks: ['image'] }, { ...spark, preferredTasks: ['video'] }], auto))).toBe('rtx');
  });

  it('then picks a server that holds the model or has room for it', () => {
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, primary: false }, { ...spark, loaded: new Set(['flux']) }], auto))).toBe('spark');
    const big = { modelMb: { flux: 24000 } };
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, ...big }, { ...spark, ...big }], auto))).toBe('spark');
  });

  it('then the less busy server, and leaves a saturated preferred server', () => {
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, jobsQueued: 3 }, spark], auto))).toBe('spark');
    const pref = { ...auto, modelPrefs: { flux: 'rtx' } };
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, jobsQueued: 2 }, spark], pref))).toBe('rtx');
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, jobsQueued: 8 }, spark], pref))).toBe('spark');
  });

  it('uses observed performance only to break remaining ties', () => {
    const perf = { rtx: { flux: 9 }, spark: { flux: 14 } };
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, primary: false }, spark], auto, perf))).toBe('rtx');
    // One unmeasured server: no speed comparison, so the stable name order decides.
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, primary: false }, spark], auto, { spark: { flux: 14 } }))).toBe('rtx');
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, primary: false }, spark], auto, { rtx: { flux: 30 }, spark: { flux: 14 } }))).toBe('spark');
  });

  it('with fallback "preferred", refuses rather than using a non-preferred server', () => {
    const policy: RoutingPolicy = { modelPrefs: { flux: 'rtx' }, fallback: 'preferred' };
    const r = route({ task: 'image', model: 'flux' }, [{ ...rtx, online: false }, spark], policy);
    expect(!r.ok && r.error).toMatch(/fallback to other servers is turned off/);
    expect(ok(route({ task: 'image', model: 'flux' }, [{ ...rtx, online: false }, spark], auto))).toBe('spark');
  });

  it('records a running average of run times', () => {
    let p = recordPerf({}, 'spark', 'flux', 10);
    p = recordPerf(p, 'spark', 'flux', 20);
    expect(p.spark.flux).toBe(13);
    expect(recordPerf(p, 'spark', 'flux', 0)).toBe(p);
  });
});
