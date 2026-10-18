<script lang="ts">
  import { Copy, Download, FileJson, FolderOpen, Library, PencilLine, RefreshCw, Repeat, Save, Shuffle, Star, Trash2, WandSparkles } from '@lucide/svelte';
  import type { Generation } from '../api/types';
  import { prefs, persistPrefs } from '../stores/prefs.svelte';
  import { copyImage, exportAs, exportMetadata, openInFolder, saveToFolder, toggleFavorite, trash } from './actions';
  import IconButton from '../ui/IconButton.svelte';
  import Button from '../ui/Button.svelte';
  import Menu, { type MenuItem } from '../ui/Menu.svelte';
  import { modelById } from '../stores/models.svelte';
  import { ago, duration } from '../util/format';

  interface Props {
    g: Generation;
    onrerun: (exact: boolean) => void;
    onvary?: (strength: number) => void;
    onedit: () => void;
    hidePrompt?: boolean;
  }
  let { g, onrerun, onvary, onedit, hidePrompt = false }: Props = $props();
  let menu = $state({ open: false, x: 0, y: 0, items: [] as MenuItem[] });

  function more(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menu = {
      open: true, x: r.left, y: r.bottom + 6, items: [
        { label: 'Export as…', icon: Download, run: () => exportAs(g) },
        { label: 'Export metadata (JSON)', icon: FileJson, run: () => exportMetadata(g) },
        ...(g.type === 'image' ? [{ label: 'Copy image', icon: Copy, run: () => copyImage(g) }] : []),
        { label: 'Open in Library', icon: Library, run: () => { prefs.view = 'library'; persistPrefs(); } },
        { separator: true },
        { label: 'Move to trash', icon: Trash2, danger: true, run: () => trash(g) },
      ],
    };
  }
  const model = $derived(modelById(g.model_id)?.name ?? g.model_id);
  const meta = $derived(g.metadata as Record<string, any>);
</script>

<div class="bar">
  <div class="info">
    {#if !hidePrompt}<p class="prompt selectable" title={g.prompt ?? ''}>{g.prompt || 'Untitled'}</p>{/if}
    <div class="meta">
      <span>{model}</span>
      <span>{g.width}×{g.height}</span>
      {#if g.type === 'video'}<span>{g.frames} frames · {g.fps} fps · {duration(g.duration)}</span>{/if}
      <span class="mono">seed {g.seed}</span>
      {#if meta?.steps}<span>{meta.steps} steps</span>{/if}
      {#if meta?.elapsed_s}<span>{duration(meta.elapsed_s)}</span>{/if}
      <span>{ago(g.created_at)}</span>
    </div>
  </div>
  <div class="acts">
    <Button size="sm" variant="ghost" onclick={onedit}><PencilLine />Edit prompt</Button>
    <Button size="sm" variant="ghost" onclick={() => onrerun(false)}><Shuffle />Regenerate</Button>
    {#if onvary}
      <Button size="sm" variant="ghost" onclick={() => onvary(0.35)}><WandSparkles />Vary subtle</Button>
      <Button size="sm" variant="ghost" onclick={() => onvary(0.6)}><RefreshCw />Vary strong</Button>
    {/if}
    <span class="sep"></span>
    <IconButton label="Rerun exactly (same seed)" onclick={() => onrerun(true)}><Repeat /></IconButton>
    <IconButton label={g.favorite ? 'Unfavorite' : 'Favorite'} active={!!g.favorite} onclick={() => toggleFavorite(g)}><Star /></IconButton>
    <IconButton label="Save to Pictures/Sarness" onclick={() => saveToFolder(g)}><Save /></IconButton>
    <IconButton label="Open in folder" onclick={() => openInFolder(g)}><FolderOpen /></IconButton>
    <IconButton label="More" onclick={more}><span class="dots">•••</span></IconButton>
  </div>
</div>

<Menu bind:open={menu.open} bind:x={menu.x} bind:y={menu.y} bind:items={menu.items} />

<style>
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; padding: 8px 0 0; min-height: 40px; }
  .info { flex: 1 1 240px; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .prompt { color: var(--text-0); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--text-3); font-size: var(--fs-xs); }
  .acts { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; }
  .sep { width: 1px; height: 18px; background: var(--line-1); margin: 0 6px; }
  .dots { font-size: 9px; letter-spacing: 1px; }
  @media (max-width: 1280px) { .acts :global(.btn.ghost) { padding: 0 8px; } }
</style>
