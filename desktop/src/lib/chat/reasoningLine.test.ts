import { describe, expect, it } from 'vitest';
import { latestReasoningLine } from './reasoningLine';

describe('latestReasoningLine', () => {
  it('returns null for empty or whitespace reasoning', () => {
    expect(latestReasoningLine('')).toBeNull();
    expect(latestReasoningLine(' \n ')).toBeNull();
  });
  it('shows the latest finished sentence until the next one is readable', () => {
    const a = latestReasoningLine('Considering the endpoints. Che');
    expect(a?.text).toBe('Considering the endpoints.');
    const b = latestReasoningLine('Considering the endpoints. Checking the available context');
    expect(b?.text).toBe('Checking the available context');
    expect(b!.key).toBeGreaterThan(a!.key);
  });
  it('keeps the key stable while the segment grows', () => {
    const a = latestReasoningLine('First thought done. Comparing the possi');
    const b = latestReasoningLine('First thought done. Comparing the possible approaches');
    expect(a?.key).toBe(b?.key);
  });
  it('splits on newlines and strips list/markdown decoration', () => {
    expect(latestReasoningLine('- **Plan** the steps\n2. Weigh the `options` carefully')?.text).toBe('Weigh the options carefully');
  });
  it('never invents text: the output is always drawn from the input words', () => {
    const src = 'Okay, the user wants a haiku about rain. I should count syllables carefully';
    const t = latestReasoningLine(src)!.text;
    expect(src.replace(/\s+/g, ' ')).toContain(t.replace(/[*_`]/g, ''));
  });
  it('is bounded for very long traces', () => {
    const long = 'word '.repeat(200_000) + 'Final conclusion reached here';
    const t0 = performance.now();
    for (let i = 0; i < 200; i++) latestReasoningLine(long);
    expect(performance.now() - t0).toBeLessThan(500);
    expect(latestReasoningLine(long)?.text).toContain('Final conclusion');
  });
});
