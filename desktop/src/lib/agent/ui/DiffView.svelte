<script lang="ts">
  let { diff, maxLines = 400 }: { diff: string; maxLines?: number } = $props();
  const lines = $derived.by(() => {
    const all = diff.replace(/\n$/, '').split('\n');
    return { shown: all.slice(0, maxLines), hidden: Math.max(0, all.length - maxLines) };
  });
  const cls = (l: string) => (l.startsWith('+++') || l.startsWith('---') ? 'meta' : l.startsWith('@@') ? 'hunk' : l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : l.startsWith('diff ') || l.startsWith('index ') ? 'meta' : '');
</script>

<pre class="diff selectable" aria-label="Diff">{#each lines.shown as l, i (i)}<span class="ln {cls(l)}">{l || ' '}</span>{/each}{#if lines.hidden}<span class="ln meta">… {lines.hidden} more lines</span>{/if}</pre>

<style>
  .diff { margin: 0; padding: 8px 0; max-height: 360px; overflow: auto; border-radius: var(--r-md); background: var(--bg-2); border: 1px solid var(--line-0); font: 12px/1.5 var(--font-mono, ui-monospace, monospace); }
  .ln { display: block; padding: 0 12px; white-space: pre; }
  .add { background: var(--ok-bg); color: var(--text-0); }
  .del { background: var(--err-bg); color: var(--text-0); }
  .hunk { color: var(--accent); background: rgba(var(--accent-rgb), 0.07); }
  .meta { color: var(--text-3); }
</style>
