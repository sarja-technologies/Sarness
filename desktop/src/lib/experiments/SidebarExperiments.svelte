<script lang="ts">
  import { LayoutGrid } from '@lucide/svelte';
  import { conn } from '../stores/connection.svelte';
  import { ago } from '../util/format';
  import { EXPERIMENTS } from './registry';
  import { exps, launch, loadDocuments, openDocument } from './store.svelte';

  /** Sidebar content while the Experiments space is open: the experiments and saved paintings. */
  let { query = '' }: { query?: string } = $props();

  $effect(() => {
    if (conn.state === 'online' && !exps.documents['gods-paintbrush']) loadDocuments('gods-paintbrush');
  });

  const paintings = $derived(
    (exps.documents['gods-paintbrush'] ?? []).filter((d) => !query.trim() || d.title.toLowerCase().includes(query.trim().toLowerCase())),
  );
</script>

<nav class="list" aria-label="Experiments">
  <button class="item" class:active={!exps.active} onclick={() => (exps.active = null)}>
    <LayoutGrid /><span class="t truncate">All experiments</span>
  </button>
  {#each EXPERIMENTS as x (x.id)}
    <button class="item" class:active={exps.active === x.id} onclick={() => launch(x.id)}>
      <x.icon /><span class="t truncate">{x.name}</span>
      {#if exps.launched.includes(x.id)}<span class="open" title="Open — its state is kept while you work elsewhere"></span>{/if}
    </button>
  {/each}

  <div class="sect">Saved paintings</div>
  {#each paintings as d (d.id)}
    <button class="item doc" onclick={() => openDocument('gods-paintbrush', d.id)} title={d.title}>
      {#if d.thumbnail}<img src={d.thumbnail} alt="" />{:else}<span class="ph"></span>{/if}
      <span class="t truncate">{d.title}</span>
      <span class="m">{ago(d.updated_at)}</span>
    </button>
  {/each}
  {#if !paintings.length}<p class="none faint">{query.trim() ? 'No matches' : 'Paintings you save appear here'}</p>{/if}
</nav>

<style>
  .list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; margin: 4px -4px 0; padding: 0 4px; }
  .sect { padding: 14px 8px 4px; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-3); }
  .item { display: flex; align-items: center; gap: 9px; width: 100%; height: 34px; padding: 0 10px; border: 0; border-radius: var(--r-md); background: none; color: var(--text-1); cursor: pointer; text-align: left; font-size: var(--fs-md); }
  .item :global(svg) { width: 15px; height: 15px; flex-shrink: 0; color: var(--text-3); }
  .item:hover { background: rgba(var(--fg-rgb), 0.05); color: var(--text-0); }
  .item.active { background: var(--select-bg); color: var(--text-0); }
  .item.active :global(svg) { color: var(--accent); }
  .t { flex: 1; min-width: 0; }
  .open { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }
  .doc { height: 40px; }
  .doc img, .ph { width: 30px; height: 20px; border-radius: 4px; object-fit: cover; flex-shrink: 0; background: var(--bg-3); box-shadow: 0 0 0 1px var(--line-1); }
  .m { font-size: 10.5px; color: var(--text-3); flex-shrink: 0; }
  .none { padding: 8px 10px; font-size: var(--fs-sm); }
</style>
