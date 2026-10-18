<script lang="ts">
  import { CircleAlert, Hourglass, Image as ImageIcon } from '@lucide/svelte';
  import Progress from '../../ui/Progress.svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import type { PaintSession } from './session.svelte';

  /** The generated landscape: the live GauGAN frame while painting, the refined photo once it matches. */
  let { session }: { session: PaintSession } = $props();

  const shown = $derived(session.show === 'refined' && session.refined ? session.refined : session.live);
  const stale = $derived(!!shown && shown.version !== session.version);
  const pct = $derived(session.progress ? session.progress.step / Math.max(1, session.progress.total) : null);
</script>

<div class="result">
  {#if shown}
    <img src={shown.url} alt="Generated landscape" draggable="false" class:stale />
  {:else if session.rendering}
    <div class="empty"><Spinner size={20} /><span>Waking the model on the server…</span></div>
  {:else if !session.error && !session.gpuBusy}
    <div class="empty"><ImageIcon /><span>Paint on the left; the landscape appears here.</span></div>
  {/if}

  {#if session.refining}
    <div class="refining glass">
      <Spinner size={13} />
      <span>{session.progress ? `Refining · step ${session.progress.step} of ${session.progress.total}` : 'Refining…'}</span>
    </div>
    <div class="bar"><Progress value={pct} height={3} /></div>
  {/if}

  {#if session.gpuBusy}
    <div class="banner warn glass"><Hourglass /><span>{session.gpuBusy}</span></div>
  {:else if session.error}
    <div class="banner err glass"><CircleAlert /><span class="selectable">{session.error}</span></div>
  {/if}
</div>

<style>
  .result {
    position: relative;
    flex: 1;
    min-height: 0;
    display: grid;
    place-items: center;
    padding: 12px;
    background: var(--bg-1);
    overflow: hidden;
  }
  img {
    position: absolute;
    inset: 12px;
    width: calc(100% - 24px);
    height: calc(100% - 24px);
    object-fit: contain;
    transition: opacity var(--t-2) var(--ease);
  }
  img.stale { opacity: 0.88; }
  .empty { display: flex; flex-direction: column; align-items: center; gap: 10px; color: var(--text-3); font-size: var(--fs-sm); text-align: center; max-width: 240px; }
  .empty :global(svg) { width: 22px; height: 22px; }
  .refining {
    position: absolute;
    top: 12px;
    right: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    border-radius: var(--r-full);
    font-size: var(--fs-xs);
    color: var(--text-1);
  }
  .bar { position: absolute; left: 0; right: 0; top: 0; }
  .banner {
    position: absolute;
    left: 12px;
    right: 12px;
    bottom: 12px;
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 9px 12px;
    border-radius: var(--r-md);
    font-size: var(--fs-sm);
    color: var(--text-0);
  }
  .banner :global(svg) { width: 16px; height: 16px; flex-shrink: 0; margin-top: 1px; }
  .banner.warn :global(svg) { color: var(--warn); }
  .banner.err :global(svg) { color: var(--err); }
</style>
