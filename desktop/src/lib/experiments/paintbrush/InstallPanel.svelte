<script lang="ts">
  import { CircleCheck, Download } from '@lucide/svelte';
  import { api } from '../../api/client';
  import { toastError } from '../../stores/toast.svelte';
  import Button from '../../ui/Button.svelte';
  import Progress from '../../ui/Progress.svelte';
  import { bytes } from '../../util/format';
  import { PB, type PaintSession } from './session.svelte';

  /** Lists the pretrained weights God's Paintbrush uses and installs the missing ones on the server. */
  let { session }: { session: PaintSession } = $props();

  const st = $derived(session.status!);
  const missing = $derived(st.components.filter((c) => !c.installed));
  const inst = $derived(st.install);
  const pct = $derived(inst.total_bytes ? inst.done_bytes / inst.total_bytes : null);
  let starting = $state(false);

  async function install(ids?: string[]) {
    starting = true;
    try {
      st.install = await api.post(`${PB}/install`, { components: ids ?? missing.map((c) => c.id) });
    } catch (e) {
      toastError(e, 'Could not start the download');
    } finally {
      starting = false;
    }
  }
</script>

<div class="install">
  <h3>{st.availability.state === 'partial' ? 'Add the refine models' : 'Set up God\'s Paintbrush'}</h3>
  <p class="muted">
    The models download once to the server ({bytes(missing.reduce((n, c) => n + c.size_bytes, 0))}) and run on its GPU.
    Nothing is installed on this computer.
  </p>
  <ul>
    {#each st.components as c (c.id)}
      <li class:done={c.installed}>
        <span class="state">{#if c.installed}<CircleCheck />{:else}<Download />{/if}</span>
        <span class="info">
          <span class="name">{c.name} <span class="faint">· {c.stage === 'live' ? 'live preview' : 'refine'} · {bytes(c.size_bytes)}</span></span>
          <span class="desc faint">{c.description}</span>
          <span class="lic">Licence: {c.license} · <a href={c.url} target="_blank" rel="noreferrer">{c.source}</a></span>
        </span>
      </li>
    {/each}
  </ul>
  {#if inst.running}
    <div class="progress">
      <span class="faint">Downloading {inst.component} · {bytes(inst.done_bytes)}{inst.total_bytes ? ` of ${bytes(inst.total_bytes)}` : ''}</span>
      <Progress value={pct} />
      <Button size="sm" variant="ghost" onclick={() => api.post(`${PB}/install/cancel`)}>Cancel</Button>
    </div>
  {:else}
    {#if inst.error}<p class="err selectable">The last download failed: {inst.error}</p>{/if}
    <div class="row">
      <Button variant="primary" loading={starting} onclick={() => install()} disabled={!missing.length}>
        Install {missing.length === st.components.length ? 'all models' : 'missing models'}
      </Button>
      {#if !st.availability.live}
        <Button variant="ghost" onclick={() => install(['spade-landscapes-512'])}>Live preview only ({bytes(st.components[0].size_bytes)})</Button>
      {/if}
    </div>
    <p class="faint small">The GauGAN landscape weights are licensed for non-commercial use only (CC BY-NC-SA 4.0).</p>
  {/if}
</div>

<style>
  .install { max-width: 560px; display: flex; flex-direction: column; gap: 12px; padding: 24px; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
  li { display: flex; gap: 10px; }
  .state :global(svg) { width: 16px; height: 16px; margin-top: 2px; color: var(--text-3); }
  .done .state :global(svg) { color: var(--ok); }
  .info { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-sm); }
  .name { font-weight: 600; color: var(--text-0); }
  .lic { font-size: var(--fs-xs); color: var(--text-2); }
  .progress { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
  .progress :global(.progress) { width: 100%; }
  .err { color: var(--err); font-size: var(--fs-sm); }
  .small { font-size: var(--fs-xs); }
</style>
