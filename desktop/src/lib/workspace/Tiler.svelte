<script lang="ts">
  import { persistWorkspace, toggleMinimize, visiblePanes, ws } from '../stores/panes.svelte';
  import { getSession } from '../stores/sessions.svelte';
  import PaneFrame from './PaneFrame.svelte';

  const vis = $derived(visiblePanes());
  const n = $derived(vis.length);
  const multi = $derived(n > 1);
  const visIds = $derived(new Set(vis.map((p) => p.id)));
  const minimized = $derived(ws.panes.filter((p) => ws.minimized.includes(p.id)));
  const rows = $derived(ws.orientation === 'rows');

  const template = $derived.by(() => {
    const c = ws.colRatio;
    const r = ws.rowRatio;
    const cols = `${c}fr ${1 - c}fr`;
    const rws = `${r}fr ${1 - r}fr`;
    if (n <= 1) return { cols: '1fr', rows: '1fr', areas: '"a"' };
    if (n === 2) return rows ? { cols: '1fr', rows: rws, areas: '"a" "b"' } : { cols, rows: '1fr', areas: '"a b"' };
    if (n === 3) return rows ? { cols, rows: rws, areas: '"a a" "b c"' } : { cols, rows: rws, areas: '"a b" "a c"' };
    return { cols, rows: rws, areas: '"a b" "c d"' };
  });
  const names = ['a', 'b', 'c', 'd'];

  const handles = $derived.by(() => {
    const out: { id: 'col' | 'row'; style: string }[] = [];
    const c = ws.colRatio * 100;
    const r = ws.rowRatio * 100;
    const col = (top = 0) => ({ id: 'col' as const, style: `left:calc(${c}% - 5px);top:${top}%;bottom:0;width:10px;cursor:col-resize` });
    const row = (left = 0) => ({ id: 'row' as const, style: `top:calc(${r}% - 5px);left:${left}%;right:0;height:10px;cursor:row-resize` });
    if (n === 2) out.push(rows ? row() : col());
    if (n === 3) out.push(...(rows ? [row(), col(r)] : [col(), row(c)]));
    if (n >= 4) out.push(col(), row());
    return out;
  });

  let grid: HTMLDivElement | undefined = $state();
  function drag(e: PointerEvent, which: 'col' | 'row') {
    if (!grid) return;
    e.preventDefault();
    const rect = grid.getBoundingClientRect();
    const move = (ev: PointerEvent) => {
      if (which === 'col') ws.colRatio = Math.min(0.8, Math.max(0.2, (ev.clientX - rect.left) / rect.width));
      else ws.rowRatio = Math.min(0.8, Math.max(0.2, (ev.clientY - rect.top) / rect.height));
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      persistWorkspace();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }
  function nudge(e: KeyboardEvent, which: 'col' | 'row') {
    const d = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -0.03 : e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 0.03 : 0;
    if (!d) return;
    e.preventDefault();
    if (which === 'col') ws.colRatio = Math.min(0.8, Math.max(0.2, ws.colRatio + d));
    else ws.rowRatio = Math.min(0.8, Math.max(0.2, ws.rowRatio + d));
    persistWorkspace();
  }
</script>

<div class="tiler" class:multi>
  {#if minimized.length}
    <div class="dock" role="toolbar" aria-label="Minimised conversations">
      {#each minimized as p (p.id)}
        <button class="chip" onclick={() => toggleMinimize(p.id)} title="Restore this conversation">{getSession(p.sessionId)?.title ?? 'Conversation'}</button>
      {/each}
    </div>
  {/if}
  <div class="stage" bind:this={grid}>
    <div class="grid" style="grid-template-columns:{template.cols};grid-template-rows:{template.rows};grid-template-areas:{template.areas}">
      {#each ws.panes as pane, i (pane.id)}
        {@const vi = vis.findIndex((p) => p.id === pane.id)}
        <PaneFrame {pane} index={i} {multi} visible={visIds.has(pane.id)} area={names[Math.max(0, vi)]} />
      {/each}
    </div>
    <div class="hlayer">
      {#each handles as h (h.id)}
        <div class="handle {h.id}" style={h.style} role="separator" tabindex="0" aria-orientation={h.id === 'col' ? 'vertical' : 'horizontal'}
          aria-label={h.id === 'col' ? 'Resize columns' : 'Resize rows'} onpointerdown={(e) => drag(e, h.id)} onkeydown={(e) => nudge(e, h.id)}
          ondblclick={() => { ws.colRatio = 0.5; ws.rowRatio = 0.5; persistWorkspace(); }}></div>
      {/each}
    </div>
  </div>
</div>

<style>
  .tiler { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  .dock { display: flex; gap: 6px; padding: 6px 8px 0; flex-shrink: 0; }
  .chip { max-width: 220px; height: 26px; padding: 0 12px; border-radius: var(--r-full); border: 1px solid var(--select-line); background: var(--select-bg); color: var(--text-0); font-size: var(--fs-xs); font-weight: 600; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .stage { position: relative; flex: 1; min-height: 0; }
  .tiler.multi .stage { margin: 8px; }
  .grid { position: absolute; inset: 0; display: grid; gap: 8px; }
  .tiler:not(.multi) .grid { gap: 0; }
  .hlayer { position: absolute; inset: 0; pointer-events: none; }
  .handle { position: absolute; z-index: 10; pointer-events: auto; }
  .handle::after { content: ''; position: absolute; inset: 3px; border-radius: 4px; background: transparent; transition: background var(--t-1); }
  .handle:hover::after, .handle:focus-visible::after { background: var(--accent); opacity: 0.55; }
</style>
