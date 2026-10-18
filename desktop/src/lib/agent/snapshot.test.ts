import { describe, expect, it } from 'vitest';
import { diffSnapshots, mergeChanges } from './snapshot';

describe('disk snapshots', () => {
  it('finds files changed by any means and flags empty ones', () => {
    const before = { 'a.txt': [1, 10], 'b.txt': [5, 10], 'gone.txt': [2, 10] } as any;
    const after = { 'a.txt': [1, 10], 'b.txt': [9, 20], 'new.js': [0, 30], 'full.js': [4, 30] } as any;
    expect(diffSnapshots(before, after)).toEqual({ created: ['full.js', 'new.js'], modified: ['b.txt'], deleted: ['gone.txt'], empty: ['new.js'] });
  });
  it('same size but different mtime counts as modified', () => {
    expect(diffSnapshots({ a: [3, 1] } as any, { a: [3, 2] } as any).modified).toEqual(['a']);
  });
  it('merges segments of one task', () => {
    const a = { created: ['x'], modified: ['m'], deleted: [], empty: ['x'] };
    const b = { created: ['y'], modified: ['x'], deleted: ['m'], empty: [] };
    expect(mergeChanges(a, b)).toEqual({ created: ['x', 'y'], modified: [], deleted: ['m'], empty: [], truncated: undefined });
  });
});
