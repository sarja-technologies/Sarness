<script lang="ts">
  import { Check, Circle, CircleDot } from '@lucide/svelte';
  let { steps }: { steps: { text: string; status: 'pending' | 'in_progress' | 'done' }[] } = $props();
  const done = $derived(steps.filter((s) => s.status === 'done').length);
</script>

<div class="plan" aria-label="Plan: {done} of {steps.length} steps done">
  <div class="h">Plan <span>{done}/{steps.length}</span></div>
  <ol>
    {#each steps as s, i (i)}
      <li class={s.status}>
        {#if s.status === 'done'}<Check />{:else if s.status === 'in_progress'}<CircleDot />{:else}<Circle />{/if}
        <span>{s.text}</span>
      </li>
    {/each}
  </ol>
</div>

<style>
  .plan { padding: 10px 14px; border: 1px solid var(--line-0); border-radius: var(--r-lg); background: var(--bg-1); }
  .h { font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-3); margin-bottom: 6px; display: flex; gap: 8px; }
  ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  li { display: flex; align-items: flex-start; gap: 8px; font-size: var(--fs-sm); color: var(--text-1); }
  li :global(svg) { width: 14px; height: 14px; margin-top: 3px; flex-shrink: 0; color: var(--text-3); }
  li.done { color: var(--text-2); }
  li.done :global(svg) { color: var(--ok); }
  li.done span { text-decoration: line-through; text-decoration-color: var(--line-2, var(--line-1)); }
  li.in_progress :global(svg) { color: var(--accent); }
  li.in_progress { color: var(--text-0); font-weight: 550; }
</style>
