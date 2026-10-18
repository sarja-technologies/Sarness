<script lang="ts">
  import Spinner from '../ui/Spinner.svelte';
  import { prefs } from '../stores/prefs.svelte';
  import ExperimentsHome from './ExperimentsHome.svelte';
  import { experimentById } from './registry';
  import { exps } from './store.svelte';

  /** Experiments space: the selection screen, plus every launched experiment kept mounted (hidden when not shown). */
  const visible = $derived(prefs.view === 'experiments');
</script>

<div class="exps">
  {#if !exps.active}<ExperimentsHome />{/if}
  {#each exps.launched as id (id)}
    {@const x = experimentById(id)}
    {#if x}
      <div class="slot" class:hidden={exps.active !== id}>
        {#await x.load()}
          <div class="loading"><Spinner size={20} /></div>
        {:then mod}
          <mod.default active={visible && exps.active === id} />
        {/await}
      </div>
    {/if}
  {/each}
</div>

<style>
  .exps { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .slot { position: absolute; inset: 0; display: flex; flex-direction: column; }
  .slot.hidden { display: none; }
  .loading { flex: 1; display: grid; place-items: center; }
</style>
