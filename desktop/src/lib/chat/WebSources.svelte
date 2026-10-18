<script lang="ts">
  import { Globe, TriangleAlert } from '@lucide/svelte';
  import type { SearchInfo } from '../api/types';

  interface Props {
    search: SearchInfo;
    /** Searching is still in progress (the answer has not started). */
    running: boolean;
    /** Render the numbered source list (under the answer) instead of the status line (above it). */
    list?: boolean;
  }
  let { search, running, list = false }: Props = $props();
  const failed = $derived(!search.sources.length && !!search.error);
</script>

{#if list}
  {#if search.sources.length}
    <div class="sources" aria-label="Sources">
      <span class="sh"><Globe />Sources</span>
      {#each search.sources as s (s.n)}
        {#if /^https?:\/\//i.test(s.url)}
          <a class="src" href={s.url} target="_blank" rel="noopener noreferrer" title="{s.title} — {s.url}"><b>{s.n}</b><span class="t">{s.title}</span><span class="d">{s.domain}</span></a>
        {/if}
      {/each}
    </div>
  {/if}
{:else if failed}
  <div class="line warn"><TriangleAlert />Web search unavailable: {search.error}. The reply below is not based on web results.</div>
{:else if running && !search.sources.length}
  <div class="line"><Globe />Searching the web for “{search.query}”…</div>
{/if}

<style>
  .line { display: inline-flex; align-items: center; gap: 6px; align-self: flex-start; color: var(--text-3); font-size: var(--fs-sm); font-style: italic; line-height: 22px; }
  .line :global(svg), .sh :global(svg) { width: 13px; height: 13px; flex: none; }
  .line.warn { color: var(--warn, var(--text-2)); }
  .sources { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 2px; }
  .sh { display: inline-flex; align-items: center; gap: 5px; color: var(--text-3); font-size: var(--fs-xs); margin-right: 2px; }
  .src {
    display: inline-flex; align-items: center; gap: 6px; max-width: 260px; padding: 3px 9px 3px 4px; border-radius: var(--r-full);
    border: 1px solid var(--line-1); background: var(--bg-2); color: var(--text-1); font-size: var(--fs-xs); text-decoration: none;
  }
  .src:hover { background: var(--bg-3); color: var(--text-0); }
  .src b { display: grid; place-items: center; min-width: 18px; height: 18px; border-radius: 9px; background: var(--bg-4, var(--bg-3)); font-weight: 600; font-size: 11px; }
  .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .d { color: var(--text-3); white-space: nowrap; }
</style>
