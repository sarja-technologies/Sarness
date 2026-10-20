<script lang="ts">
  import { Copy, Pencil, Trash2 } from '@lucide/svelte';
  import { toastError } from '../../stores/toast.svelte';
  import Empty from '../../ui/Empty.svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import Modal from '../../ui/Modal.svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import { confirmDialog, promptDialog } from '../../ui/dialogs.svelte';
  import { ago } from '../../util/format';
  import { exps, loadDocuments, xapi } from '../store.svelte';
  import { PB } from './session.svelte';

  /** Saved paintings on the server (newest first). */
  let { open = $bindable(), onopen, current }: { open: boolean; onopen: (id: string) => void; current: string | null } = $props();

  let loading = $state(false);
  const docs = $derived(exps.documents['gods-paintbrush'] ?? []);

  $effect(() => {
    if (open) {
      loading = !exps.documents['gods-paintbrush'];
      loadDocuments('gods-paintbrush').finally(() => (loading = false));
    }
  });

  async function act(fn: () => Promise<unknown>, what: string) {
    try {
      await fn();
      await loadDocuments('gods-paintbrush');
    } catch (e) {
      toastError(e, what);
    }
  }

  async function rename(id: string, title: string) {
    const t = await promptDialog('Rename painting', title, { confirmLabel: 'Rename' });
    if (t?.trim()) act(() => xapi().put(`${PB}/documents/${id}`, { title: t.trim() }), 'Could not rename');
  }

  async function remove(id: string, title: string) {
    if (await confirmDialog(`Delete “${title}”?`, 'The painting is removed from the server. Images saved to the Library stay.', { confirmLabel: 'Delete', danger: true })) {
      act(() => xapi().del(`${PB}/documents/${id}`), 'Could not delete');
    }
  }
</script>

<Modal bind:open title="Open a painting" subtitle="Paintings are saved on your Sarness server" width={760}>
  {#if loading}
    <div class="center"><Spinner /></div>
  {:else if !docs.length}
    <Empty compact title="No saved paintings yet" body="Use Save in the toolbar to keep a painting on the server." />
  {:else}
    <div class="grid">
      {#each docs as d (d.id)}
        <div class="card" class:current={d.id === current}>
          <button class="thumb" onclick={() => { open = false; onopen(d.id); }} aria-label={`Open ${d.title}`}>
            {#if d.thumbnail}<img src={d.thumbnail} alt="" />{:else}<span class="none faint">No preview</span>{/if}
          </button>
          <div class="row">
            <span class="title truncate" title={d.title}>{d.title}</span>
            <IconButton label="Rename" size="sm" onclick={() => rename(d.id, d.title)}><Pencil /></IconButton>
            <IconButton label="Duplicate" size="sm" onclick={() => act(() => xapi().post(`${PB}/documents/${d.id}/duplicate`), 'Could not duplicate')}><Copy /></IconButton>
            <IconButton label="Delete" size="sm" tone="danger" onclick={() => remove(d.id, d.title)}><Trash2 /></IconButton>
          </div>
          <span class="when faint">{d.id === current ? 'Open now · ' : ''}edited {ago(d.updated_at)}</span>
        </div>
      {/each}
    </div>
  {/if}
</Modal>

<style>
  .center { display: grid; place-items: center; padding: 40px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; max-height: 60vh; overflow-y: auto; padding: 2px; }
  .card { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .thumb {
    aspect-ratio: 3 / 2;
    padding: 0;
    border: 1px solid var(--line-1);
    border-radius: var(--r-md);
    overflow: hidden;
    background: var(--bg-2);
    cursor: pointer;
    display: grid;
    place-items: center;
  }
  .thumb:hover { border-color: var(--accent); }
  .current .thumb { border-color: var(--select-line); box-shadow: 0 0 0 2px var(--select-bg); }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .none { font-size: var(--fs-xs); }
  .row { display: flex; align-items: center; gap: 0; }
  .title { flex: 1; min-width: 0; font-weight: 600; font-size: var(--fs-sm); }
  .when { font-size: var(--fs-xs); }
</style>
