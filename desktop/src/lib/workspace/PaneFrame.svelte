<script lang="ts">
  import { ws, focusPane, setPaneSession, swapPanes, type Pane } from '../stores/panes.svelte';
  import { getSession, newSession } from '../stores/sessions.svelte';
  import Session from './Session.svelte';
  import SessionBar from './SessionBar.svelte';

  let { pane, visible, area, index, multi }: { pane: Pane; visible: boolean; area: string; index: number; multi: boolean } = $props();

  const session = $derived(getSession(pane.sessionId));
  const focused = $derived(ws.focus === pane.id);
  let dropTarget = $state(false);

  function dragStart(e: DragEvent) {
    e.dataTransfer?.setData('application/x-sarness-pane', pane.id);
    if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
  }
  function over(e: DragEvent) {
    if (e.dataTransfer?.types.includes('application/x-sarness-pane')) {
      e.preventDefault();
      dropTarget = true;
    }
  }
  function drop(e: DragEvent) {
    dropTarget = false;
    const from = e.dataTransfer?.getData('application/x-sarness-pane');
    if (from) {
      e.preventDefault();
      swapPanes(from, pane.id);
    }
  }
  // A deleted conversation is replaced by a fresh one in the same pane.
  function removed() {
    setPaneSession(pane.id, newSession().id);
  }
  // The session this pane points at can vanish (deleted elsewhere): recover with a blank one.
  $effect(() => {
    if (!session) setPaneSession(pane.id, newSession().id);
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_click_events_have_key_events -->
<section class="pane" class:multi class:focused={multi && focused} class:drop={dropTarget} class:hidden={!visible} style="grid-area:{area}"
  aria-label="Conversation {index + 1}" onpointerdown={() => focusPane(pane.id)} onfocusin={() => focusPane(pane.id)}
  ondragover={over} ondragleave={() => (dropTarget = false)} ondrop={drop}>
  {#if session}
    <SessionBar {session} paneId={pane.id} {multi} onremoved={removed} ondragstart={dragStart} />
    <div class="body">
      {#key session.id}
        <Session sessionId={session.id} active={visible} focused={focused && visible} />
      {/key}
    </div>
  {/if}
</section>

<style>
  .pane { position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--bg-0); overflow: hidden; }
  .pane.multi { border: 1px solid var(--line-1); border-radius: var(--r-lg); transition: border-color var(--t-1); }
  .pane.focused { border-color: var(--select-line); }
  .pane.drop { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-ghost); }
  .pane.hidden { display: none; }
  .body { flex: 1; min-height: 0; position: relative; }
  .body > :global(*) { height: 100%; }
</style>
