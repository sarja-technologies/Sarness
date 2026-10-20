<script lang="ts">
  import { X } from '@lucide/svelte';
  import type { Job, Workflow } from '../api/types';
  import WorkflowEditor from '../workflow/WorkflowEditor.svelte';
  import IconButton from '../ui/IconButton.svelte';

  /** Optional advanced interface: the node editor behind a full-window overlay. */
  let { kind, graph = $bindable(), engine = null, onrun, onrebuild, onclose }: {
    kind: 'image' | 'video';
    /** Server whose nodes and models the graph uses (null = primary). */
    engine?: string | null;
    graph: Workflow | null;
    onrun: (g: Workflow) => Promise<Job>;
    onrebuild: () => Promise<void>;
    onclose: () => void;
  } = $props();
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && !(e.target as HTMLElement)?.closest('input, textarea, select')) onclose(); }} />

<div class="overlay" role="dialog" aria-modal="true" aria-label="Node workflow editor">
  <header>
    <h2>Node workflow · {kind}</h2>
    <span class="faint">Advanced. Results are added to this conversation.</span>
    <div class="spacer"></div>
    <IconButton label="Close editor  ·  Esc" onclick={onclose}><X /></IconButton>
  </header>
  <div class="body"><WorkflowEditor {kind} bind:graph {engine} active={true} {onrun} {onrebuild} /></div>
</div>

<style>
  .overlay { position: fixed; inset: 0; z-index: 500; display: flex; flex-direction: column; background: var(--bg-0); animation: fade-in var(--t-2) var(--ease); }
  header { display: flex; align-items: center; gap: 12px; height: 48px; padding: 0 12px 0 20px; border-bottom: 1px solid var(--line-1); flex-shrink: 0; }
  h2 { font-size: var(--fs-lg); }
  .body { flex: 1; min-height: 0; display: flex; }
</style>
