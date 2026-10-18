<script lang="ts">
  import { ShieldAlert, ShieldQuestion } from '@lucide/svelte';
  import Button from '../../ui/Button.svelte';
  import type { ApprovalInfo } from '../types';
  import DiffView from './DiffView.svelte';

  let { info, onresolve }: { info: ApprovalInfo; onresolve: (ok: boolean) => void } = $props();
  const risky = $derived(info.risk === 'high' || info.risk === 'critical');
  let denyBtn: HTMLButtonElement | undefined = $state();
  // Risky actions: focus lands on Deny so Enter can never approve by accident.
  $effect(() => {
    if (info.state === 'waiting' && risky) denyBtn?.focus();
  });
</script>

<div class="ap" class:risky role="alertdialog" aria-label="Permission request: {info.title}" tabindex="-1"
  onkeydown={(e) => { if (e.key === 'Escape' && info.state === 'waiting') onresolve(false); }}>
  <div class="head">
    {#if risky}<ShieldAlert />{:else}<ShieldQuestion />{/if}
    <strong>{info.title}</strong>
    <span class="risk r-{info.risk}">{info.risk} risk</span>
  </div>
  <p class="reason">{info.reason}</p>
  {#if info.preview?.kind === 'diff' && info.preview.diff}
    <DiffView diff={info.preview.diff} maxLines={80} />
  {:else if info.preview?.kind === 'command'}
    <pre class="cmd selectable">$ {info.preview.command}{info.preview.cwd ? `\n(in ${info.preview.cwd})` : ''}</pre>
  {:else if info.preview?.kind === 'move'}
    <pre class="cmd selectable">{info.preview.from} → {info.preview.to}</pre>
  {:else if info.preview?.kind === 'delete'}
    <pre class="cmd selectable">delete {info.preview.path}</pre>
  {:else if info.preview?.kind === 'commit'}
    <pre class="cmd selectable">git commit -m {JSON.stringify(info.preview.message)}</pre>
  {/if}
  {#if info.state === 'waiting'}
    <div class="acts">
      <Button size="sm" variant="primary" onclick={() => onresolve(true)}>Approve</Button>
      <button bind:this={denyBtn} type="button" class="deny" onclick={() => onresolve(false)}>Deny</button>
      <span class="faint">Esc denies</span>
    </div>
  {:else}
    <div class="faint">{info.state === 'approved' ? 'Approved' : 'Denied'}</div>
  {/if}
</div>

<style>
  .ap { margin: 6px 0 2px; padding: 12px 14px; border-radius: var(--r-lg); border: 1px solid var(--warn); background: var(--warn-bg); display: flex; flex-direction: column; gap: 8px; }
  .ap.risky { border-color: var(--err); background: var(--err-bg); }
  .head { display: flex; align-items: center; gap: 8px; }
  .head :global(svg) { width: 17px; height: 17px; color: var(--warn); }
  .risky .head :global(svg) { color: var(--err); }
  .risk { margin-left: auto; font-size: var(--fs-xs); padding: 1px 8px; border-radius: var(--r-full); border: 1px solid var(--line-1); color: var(--text-2); text-transform: capitalize; }
  .r-high, .r-critical { color: var(--err); border-color: var(--err); }
  .reason { margin: 0; color: var(--text-1); font-size: var(--fs-sm); }
  .cmd { margin: 0; padding: 8px 12px; border-radius: var(--r-md); background: var(--bg-2); border: 1px solid var(--line-0); font: 12px/1.5 var(--font-mono, ui-monospace, monospace); white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow: auto; }
  .acts { display: flex; align-items: center; gap: 8px; }
  .deny { height: 28px; padding: 0 14px; border-radius: var(--r-md); border: 1px solid var(--line-2, var(--line-1)); background: var(--bg-0); color: var(--text-0); cursor: pointer; font-size: var(--fs-sm); }
  .deny:hover { background: var(--bg-2); }
  .faint { color: var(--text-3); font-size: var(--fs-xs); }
</style>
