<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { ChevronRight } from '@lucide/svelte';
  import { prefs } from '../stores/prefs.svelte';
  import { latestReasoningLine } from './reasoningLine';

  interface Props {
    /** Reasoning text the model itself emitted for this message. Empty when the model exposes none. */
    reasoning: string;
    /** This message is still being generated. */
    streaming: boolean;
    /** The answer proper has started; reasoning is over. */
    answering: boolean;
  }
  let { reasoning, streaming, answering }: Props = $props();

  const live = $derived(streaming && !answering && reasoning.length > 0);
  const waiting = $derived(streaming && !answering && reasoning.length === 0);
  const line = $derived(live ? latestReasoningLine(reasoning) : null);

  // Only the first render decides the default; afterwards the reader (or the end of reasoning) does.
  let open = $state(untrack(() => !streaming && prefs.chatShowReasoning));
  let touched = $state(false);
  let wasLive = false;
  let panel = $state<HTMLElement | null>(null);
  let pinned = true;

  // When reasoning ends, fold the trace again unless the reader opened it on purpose.
  $effect(() => {
    if (live) wasLive = true;
    else if (wasLive) {
      wasLive = false;
      if (!touched) open = false;
    }
  });

  // Follow the live trace while the reader is at its end; leave it alone once they scroll up.
  $effect(() => {
    void reasoning.length;
    if (open && pinned && panel) tick().then(() => panel && (panel.scrollTop = panel.scrollHeight));
  });

  function toggle() {
    touched = true;
    open = !open;
    pinned = true;
  }
  function onscroll() {
    if (panel) pinned = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 24;
  }
</script>

{#if reasoning}
  <div class="reason">
    <button class="head" class:muted-line={live} onclick={toggle} aria-expanded={open} aria-controls="trace">
      {#if live && line}
        {#key line.key}<span class="line">{line.text}</span>{/key}
      {:else if live}
        <span class="line">Thinking…</span>
      {:else}
        <span class="see">{open ? 'Hide thinking' : 'See thinking'}</span>
      {/if}
      <ChevronRight class="chev {open ? 'open' : ''}" />
    </button>
    {#if open}
      <div id="trace" class="trace selectable" bind:this={panel} {onscroll}>{reasoning}</div>
    {/if}
  </div>
{:else if waiting}
  <div class="reason"><span class="wait">Generating…</span></div>
{/if}

<style>
  .reason { display: flex; flex-direction: column; gap: 6px; align-self: flex-start; max-width: 100%; }
  .head {
    display: inline-flex; align-items: center; gap: 4px; max-width: 100%; min-height: 22px; padding: 0; border: 0; background: none;
    color: var(--text-3); font-size: var(--fs-sm); font-style: italic; line-height: 22px; text-align: left; cursor: pointer;
  }
  .head:hover { color: var(--text-2); }
  .head:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .line { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; animation: line-in var(--t-3, 220ms) var(--ease-out, ease-out); }
  :global(.chev) { flex: none; width: 13px; height: 13px; transition: transform var(--t-2, 160ms) var(--ease, ease); }
  :global(.chev.open) { transform: rotate(90deg); }
  .trace {
    max-height: 260px; overflow-y: auto; padding: 4px 0 4px 12px; border-left: 2px solid var(--line-2);
    color: var(--text-3); font-size: var(--fs-sm); font-style: italic; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .wait { color: var(--text-3); font-size: var(--fs-sm); font-style: italic; line-height: 22px; animation: breathe 1.8s ease-in-out infinite; }
  @keyframes line-in { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
  @keyframes breathe { 50% { opacity: 0.45; } }
  @media (prefers-reduced-motion: reduce) { .line, .wait { animation: none; } :global(.chev) { transition: none; } }
</style>
