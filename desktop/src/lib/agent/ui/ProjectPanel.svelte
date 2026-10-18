<script lang="ts">
  import { ChevronRight, File, Folder, GitBranch, RefreshCw, X } from '@lucide/svelte';
  import { native, type GitState, type TreeNode } from '../native';
  import { workspaceTitle } from '../prompt';
  import DiffView from './DiffView.svelte';

  let { project, focusFile = null, refreshKey = 0, onclose }: { project: string; focusFile?: string | null; refreshKey?: number; onclose: () => void } = $props();
  let tree = $state<TreeNode | null>(null);
  let git = $state<GitState | null>(null);
  let diff = $state('');
  let selected = $state<string | null>(null);
  let tab = $state<'changes' | 'files'>('changes');
  let busy = $state(false);
  let err = $state('');
  let seq = 0;

  async function load() {
    const my = ++seq;
    busy = true;
    err = '';
    try {
      const [t, g] = await Promise.all([native.tree(project, 3), native.git(project)]);
      if (my !== seq) return;
      tree = t;
      git = g;
      await showDiff(selected);
    } catch (e) {
      err = (e as Error).message ?? String(e);
    } finally {
      if (my === seq) busy = false;
    }
  }
  async function showDiff(file: string | null) {
    selected = file;
    try {
      diff = git?.repo || true ? await native.diff(project, file ?? undefined) : '';
    } catch (e) {
      diff = '';
      err = (e as Error).message ?? String(e);
    }
  }
  $effect(() => {
    void project;
    void refreshKey;
    void load();
  });
  $effect(() => {
    if (focusFile !== null && focusFile !== undefined) {
      tab = 'changes';
      void showDiff(focusFile || null);
    }
  });
  let open = $state<Record<string, boolean>>({});
</script>

{#snippet node(n: TreeNode, path: string, depth: number)}
  {#each n.children ?? [] as c (c.name)}
    {@const p = path ? `${path}/${c.name}` : c.name}
    {#if c.dir}
      <button type="button" class="tn" style="padding-left:{8 + depth * 12}px" aria-expanded={!!open[p]} onclick={() => (open[p] = !open[p])}>
        <ChevronRight class="chev {open[p] ? 'open' : ''}" /><Folder /><span class="truncate">{c.name}</span>
      </button>
      {#if open[p]}{@render node(c, p, depth + 1)}{/if}
    {:else}
      <button type="button" class="tn" class:sel={selected === p} style="padding-left:{22 + depth * 12}px" onclick={() => { tab = 'changes'; void showDiff(p); }}>
        <File /><span class="truncate">{c.name}</span>
      </button>
    {/if}
  {/each}
{/snippet}

<aside class="panel" aria-label="Project: {workspaceTitle(project)}">
  <header>
    <div class="ttl"><strong class="truncate">{workspaceTitle(project)}</strong><span class="path truncate selectable">{project}</span></div>
    <button type="button" class="ib" onclick={load} aria-label="Refresh" title="Refresh"><RefreshCw class={busy ? 'spin' : ''} /></button>
    <button type="button" class="ib" onclick={onclose} aria-label="Close panel"><X /></button>
  </header>
  <div class="tabs" role="tablist">
    <button role="tab" aria-selected={tab === 'changes'} class:on={tab === 'changes'} onclick={() => (tab = 'changes')}>Changes{#if git?.files?.length} <em>{git.files.length}</em>{/if}</button>
    <button role="tab" aria-selected={tab === 'files'} class:on={tab === 'files'} onclick={() => (tab = 'files')}>Files</button>
    {#if git?.repo}<span class="br"><GitBranch />{(git.branch ?? '').split('...')[0]}</span>{/if}
  </div>
  {#if err}<p class="err">{err}</p>{/if}
  <div class="body">
    {#if tab === 'files'}
      <div class="tree">{#if tree}{@render node(tree, '', 0)}{/if}</div>
    {:else}
      {#if git && !git.repo}
        <p class="faint pad">Not a Git repository, so changes cannot be diffed against a baseline. Ask the agent to run <code>git init</code> to track changes.</p>
      {:else if git?.files?.length}
        <ul class="chg">
          <li><button type="button" class:sel={selected === null} onclick={() => showDiff(null)}>All changes</button></li>
          {#each git.files as f (f.path)}
            <li><button type="button" class:sel={selected === f.path} onclick={() => showDiff(f.path)}><span class="gs">{f.status}</span><span class="truncate">{f.path}</span></button></li>
          {/each}
        </ul>
      {:else if git}
        <p class="faint pad">Working tree clean.</p>
      {/if}
      {#if diff}<div class="dv"><DiffView {diff} maxLines={1500} /></div>{/if}
    {/if}
  </div>
</aside>

<style>
  .panel { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--bg-0); }
  header { display: flex; align-items: center; gap: 4px; padding: 10px 10px 6px 14px; }
  .ttl { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .path { font-size: var(--fs-xs); color: var(--text-3); }
  .ib { display: grid; place-items: center; width: 28px; height: 28px; border: 0; background: none; border-radius: 8px; color: var(--text-2); cursor: pointer; }
  .ib:hover { background: rgba(var(--fg-rgb), 0.06); }
  .ib :global(svg) { width: 15px; height: 15px; }
  .ib :global(.spin) { animation: spin 0.9s linear infinite; }
  .tabs { display: flex; align-items: center; gap: 2px; padding: 0 10px; border-bottom: 1px solid var(--line-0); }
  .tabs button { border: 0; background: none; padding: 8px 10px; color: var(--text-2); cursor: pointer; font-size: var(--fs-sm); border-bottom: 2px solid transparent; }
  .tabs button.on { color: var(--text-0); border-bottom-color: var(--accent); }
  .tabs em { font-style: normal; font-size: var(--fs-xs); background: var(--bg-3); border-radius: var(--r-full); padding: 0 6px; }
  .br { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-xs); color: var(--text-3); }
  .br :global(svg) { width: 12px; height: 12px; }
  .body { flex: 1; min-height: 0; overflow: auto; }
  .tree { padding: 6px 0; }
  .tn { display: flex; align-items: center; gap: 6px; width: 100%; height: 26px; border: 0; background: none; color: var(--text-1); font-size: 12.5px; cursor: pointer; text-align: left; padding-right: 8px; }
  .tn:hover, .tn.sel { background: var(--select-bg); }
  .tn :global(svg) { width: 13px; height: 13px; color: var(--text-3); flex-shrink: 0; }
  .tn :global(.chev) { transition: transform var(--t-1); }
  .tn :global(.chev.open) { transform: rotate(90deg); }
  .chg { list-style: none; margin: 0; padding: 6px; display: flex; flex-direction: column; gap: 1px; max-height: 40%; overflow: auto; }
  .chg button { display: flex; gap: 8px; width: 100%; padding: 4px 8px; border: 0; background: none; border-radius: 6px; color: var(--text-1); font: 12.5px var(--font-mono, ui-monospace, monospace); cursor: pointer; text-align: left; }
  .chg button:hover, .chg button.sel { background: var(--select-bg); }
  .gs { width: 18px; color: var(--accent); }
  .dv { padding: 0 10px 12px; }
  .dv :global(.diff) { max-height: none; }
  .pad { padding: 14px; }
  .faint { color: var(--text-3); font-size: var(--fs-sm); }
  .err { margin: 8px 14px; color: var(--err); font-size: var(--fs-sm); }
</style>
