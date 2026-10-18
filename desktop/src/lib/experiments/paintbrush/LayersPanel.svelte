<script lang="ts">
  import { ChevronDown, ChevronUp, Eraser, Eye, EyeOff, Plus, Trash2 } from '@lucide/svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import Select from '../../ui/Select.svelte';
  import { promptDialog } from '../../ui/dialogs.svelte';
  import type { PaintSession } from './session.svelte';

  /** Layers paint over each other (top wins); unpainted areas show the background material. */
  let { session }: { session: PaintSession } = $props();

  const top = $derived([...session.layers].reverse());
  const bgOptions = $derived(session.materials.map((m) => ({ value: m.index, label: m.label })));
  const background = $derived(session.canvas ? (session.version, session.canvas.background) : 0);

  async function rename(id: string, name: string) {
    const next = await promptDialog('Rename layer', name, { confirmLabel: 'Rename' });
    if (next?.trim()) session.renameLayer(id, next.trim());
  }
</script>

<div class="layers">
  <div class="head">
    <span class="eyebrow">Layers</span>
    <span class="spacer"></span>
    <IconButton label="New layer" size="sm" onclick={() => session.addLayer()}><Plus /></IconButton>
  </div>
  <div class="list" role="listbox" aria-label="Layers">
    {#each top as l, i (l.id)}
      <div
        class="layer"
        class:on={l.id === session.activeLayer}
        role="option"
        aria-selected={l.id === session.activeLayer}
        tabindex="0"
        onclick={() => (session.activeLayer = l.id)}
        onkeydown={(e) => e.key === 'Enter' && (session.activeLayer = l.id)}
        ondblclick={() => rename(l.id, l.name)}
      >
        <IconButton label={l.visible ? 'Hide layer' : 'Show layer'} size="sm" tip={false}
                    onclick={(e) => { e.stopPropagation(); session.toggleLayer(l.id); }}>
          {#if l.visible}<Eye />{:else}<EyeOff />{/if}
        </IconButton>
        <span class="name truncate" class:hidden={!l.visible}>{l.name}</span>
        {#if l.id === session.activeLayer}
          <span class="acts">
            <IconButton label="Move up" size="sm" disabled={i === 0} onclick={(e) => { e.stopPropagation(); session.moveLayer(l.id, 1); }}><ChevronUp /></IconButton>
            <IconButton label="Move down" size="sm" disabled={i === top.length - 1} onclick={(e) => { e.stopPropagation(); session.moveLayer(l.id, -1); }}><ChevronDown /></IconButton>
            <IconButton label="Clear layer" size="sm" onclick={(e) => { e.stopPropagation(); session.clear(false); }}><Eraser /></IconButton>
            <IconButton label="Delete layer" size="sm" tone="danger" disabled={top.length <= 1} onclick={(e) => { e.stopPropagation(); session.removeLayer(l.id); }}><Trash2 /></IconButton>
          </span>
        {/if}
      </div>
    {/each}
  </div>
  <div class="bg">
    <span class="faint">Background</span>
    <Select size="sm" value={background} options={bgOptions} onchange={(v) => session.setBackground(v)} width="130px" />
  </div>
</div>

<style>
  .layers { display: flex; flex-direction: column; gap: 6px; }
  .head { display: flex; align-items: center; }
  .list { display: flex; flex-direction: column; gap: 2px; }
  .layer {
    display: flex;
    align-items: center;
    gap: 4px;
    height: 32px;
    padding: 0 4px 0 2px;
    border-radius: var(--r-sm);
    border: 1px solid transparent;
    cursor: pointer;
    font-size: var(--fs-sm);
    color: var(--text-1);
  }
  .layer:hover { background: rgba(var(--fg-rgb), 0.04); }
  .layer.on { background: var(--select-bg); border-color: var(--select-line); color: var(--text-0); }
  .name { flex: 1; min-width: 0; }
  .name.hidden { color: var(--text-3); text-decoration: line-through; }
  .acts { display: flex; gap: 0; }
  .bg { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: var(--fs-sm); padding: 4px 2px 0; }
</style>
