// Compares two file snapshots (path -> [size, mtime]) so the task summary reflects what really
// changed on disk, no matter whether a file tool or a shell command (cat > f, sed -i, npm …) did it.
export type Snapshot = Record<string, [number, number]>;

export interface DiskChanges {
  created: string[];
  modified: string[];
  deleted: string[];
  empty: string[]; // created or modified files that are 0 bytes right now
  truncated?: boolean;
}

export const noChanges = (): DiskChanges => ({ created: [], modified: [], deleted: [], empty: [] });

export function diffSnapshots(before: Snapshot, after: Snapshot): DiskChanges {
  const out = noChanges();
  for (const [p, [size, mt]] of Object.entries(after)) {
    const b = before[p];
    if (!b) out.created.push(p);
    else if (b[0] !== size || b[1] !== mt) out.modified.push(p);
    else continue;
    if (size === 0) out.empty.push(p);
  }
  for (const p of Object.keys(before)) if (!(p in after)) out.deleted.push(p);
  for (const k of ['created', 'modified', 'deleted', 'empty'] as const) out[k].sort();
  return out;
}

/** Merges the changes of consecutive segments of one task (e.g. before/after "Continue task"). */
export function mergeChanges(a: DiskChanges | undefined, b: DiskChanges): DiskChanges {
  if (!a) return b;
  const created = new Set([...a.created, ...b.created]);
  const deleted = new Set([...a.deleted, ...b.deleted]);
  const modified = new Set([...a.modified, ...b.modified]);
  for (const p of deleted) {
    created.delete(p);
    modified.delete(p);
  }
  for (const p of created) modified.delete(p);
  const empty = new Set(b.empty);
  return { created: [...created].sort(), modified: [...modified].sort(), deleted: [...deleted].sort(), empty: [...empty].filter((p) => !deleted.has(p)).sort(), truncated: a.truncated || b.truncated };
}
