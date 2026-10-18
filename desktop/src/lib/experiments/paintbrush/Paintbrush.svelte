<script lang="ts">
  import { ArrowLeft, Brush, Eraser, FilePlus, FolderOpen, Hand, PaintBucket, Pipette, Redo2, RefreshCw, Save, Trash2, Undo2 } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import Button from '../../ui/Button.svelte';
  import Empty from '../../ui/Empty.svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import Modal from '../../ui/Modal.svelte';
  import Segmented from '../../ui/Segmented.svelte';
  import Slider from '../../ui/Slider.svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import { confirmDialog, promptDialog } from '../../ui/dialogs.svelte';
  import { tooltip } from '../../ui/tooltip';
  import { exps } from '../store.svelte';
  import GeneratePanel from './GeneratePanel.svelte';
  import InstallPanel from './InstallPanel.svelte';
  import LayersPanel from './LayersPanel.svelte';
  import MaterialPicker from './MaterialPicker.svelte';
  import OpenDialog from './OpenDialog.svelte';
  import PaintCanvas from './PaintCanvas.svelte';
  import ResultPanel from './ResultPanel.svelte';
  import { PaintSession, type Tool } from './session.svelte';

  let { active = true }: { active?: boolean } = $props();

  const session = new PaintSession();
  let openDialog = $state(false);
  let newDialog = $state(false);
  let installDialog = $state(false);
  let newSize = $state('landscape');

  onMount(() => {
    void session.init();
    return () => session.destroy();
  });

  const st = $derived(session.status);
  const avail = $derived(st?.availability.state);
  const runtime = $derived(st?.runtime);
  const tools: { id: Tool; label: string; key: string; icon: any }[] = [
    { id: 'brush', label: 'Brush', key: 'B', icon: Brush },
    { id: 'eraser', label: 'Eraser', key: 'E', icon: Eraser },
    { id: 'fill', label: 'Fill', key: 'G', icon: PaintBucket },
    { id: 'picker', label: 'Pick material', key: 'I', icon: Pipette },
    { id: 'pan', label: 'Pan (or hold Space)', key: 'H', icon: Hand },
  ];

  // A saved painting picked in the sidebar.
  $effect(() => {
    const r = exps.openRequest;
    if (r && r.experiment === 'gods-paintbrush' && session.status && session.width) {
      exps.openRequest = null;
      void openDoc(r.documentId);
    }
  });

  async function discardOk(): Promise<boolean> {
    if (!session.doc.dirty) return true;
    return confirmDialog('Discard unsaved changes?', `“${session.doc.title}” has changes that are not saved on the server.`,
                         { confirmLabel: 'Discard', danger: true });
  }

  async function openDoc(id: string) {
    if (id === session.doc.id && !session.doc.dirty) return;
    if (await discardOk()) await session.open(id);
  }

  async function createNew() {
    if (!(await discardOk())) return;
    const size = st?.canvas.sizes.find((x) => x.id === newSize) ?? st?.canvas.sizes[0];
    newDialog = false;
    if (size) session.newPainting(size.width, size.height);
  }

  async function rename() {
    const t = await promptDialog('Rename painting', session.doc.title, { confirmLabel: 'Rename' });
    if (!t?.trim()) return;
    session.doc.title = t.trim();
    if (session.doc.id) await session.save();
    else session.doc.dirty = true;
  }

  function key(e: KeyboardEvent) {
    if (!active || !session.width || openDialog || newDialog || installDialog) return;
    const t = e.target as HTMLElement;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    const k = e.key.toLowerCase();
    const mod = e.ctrlKey || e.metaKey;
    if (mod && k === 'z') {
      if (e.shiftKey) session.redo();
      else session.undo();
    } else if (mod && k === 'y') session.redo();
    else if (mod && k === 's') void session.save({ asCopy: e.shiftKey });
    else if (mod && k === 'enter') void session.refine();
    else if (!mod && !e.altKey) {
      const tool = tools.find((x) => x.key.toLowerCase() === k);
      if (tool) session.tool = tool.id;
      else if (k === '[') session.brush = Math.max(2, Math.round(session.brush / 1.2));
      else if (k === ']') session.brush = Math.min(200, Math.round(session.brush * 1.2) + 1);
      else return;
    } else return;
    e.preventDefault();
  }
</script>

<svelte:window onkeydown={key} />

<div class="pb">
  <header class="bar">
    <IconButton label="All experiments" onclick={() => (exps.active = null)}><ArrowLeft /></IconButton>
    <div class="titles">
      <span class="eyebrow">God's Paintbrush</span>
      <button class="doc" onclick={rename} use:tooltip={'Rename'}>
        <span class="truncate">{session.doc.title}</span>
        {#if session.doc.dirty}<span class="dot" title="Unsaved changes"></span>{/if}
      </button>
    </div>
    <span class="spacer"></span>
    {#if st && session.width}
      <Button size="sm" variant="ghost" onclick={() => (newDialog = true)}><FilePlus />New</Button>
      <Button size="sm" variant="ghost" onclick={() => (openDialog = true)}><FolderOpen />Open</Button>
      <Button size="sm" variant="ghost" onclick={() => session.save({ asCopy: true })} disabled={session.saving}>Save a copy</Button>
      <Button size="sm" variant={session.doc.dirty || !session.doc.id ? 'primary' : 'secondary'} loading={session.saving} onclick={() => session.save()}>
        <Save />Save
      </Button>
    {/if}
    {#if st}
      <span class="gpu" use:tooltip={runtime?.worker ? `Model worker running on ${runtime.device ?? 'the server GPU'}${runtime.vram_reserved_mb ? ` · ${(runtime.vram_reserved_mb / 1024).toFixed(1)} GB reserved` : ''}` : 'Models load on the server GPU on first use and unload when idle'}>
        <span class="led" class:on={runtime?.worker} class:busy={!!session.gpuBusy}></span>
        {st.gpu.name ?? 'Server GPU'}
      </span>
    {/if}
  </header>

  {#if session.statusError}
    <div class="center">
      <Empty title="God's Paintbrush is not reachable" body={session.statusError}>
        <Button onclick={() => session.init()}><RefreshCw />Try again</Button>
      </Empty>
    </div>
  {:else if !st}
    <div class="center"><Spinner size={22} /></div>
  {:else if avail === 'unavailable'}
    <div class="center"><Empty title="Not available on this server" body={st.availability.reasons.join(' ')} /></div>
  {:else if avail === 'needs_install'}
    <div class="center scroll"><InstallPanel {session} /></div>
  {:else if !session.width}
    <div class="center"><Spinner size={22} /></div>
  {:else}
    <div class="body">
      <aside class="left">
        <div class="tools" role="toolbar" aria-label="Tools">
          {#each tools as t (t.id)}
            <IconButton label={`${t.label}  ·  ${t.key}`} active={session.tool === t.id} onclick={() => (session.tool = t.id)}><t.icon /></IconButton>
          {/each}
        </div>
        <Slider bind:value={session.brush} min={2} max={200} label="Brush size" unit=" px" />
        <div class="row">
          <IconButton label="Undo  ·  Ctrl+Z" disabled={!session.canUndo} onclick={() => session.undo()}><Undo2 /></IconButton>
          <IconButton label="Redo  ·  Ctrl+Shift+Z" disabled={!session.canRedo} onclick={() => session.redo()}><Redo2 /></IconButton>
          <span class="spacer"></span>
          <Button size="sm" variant="ghost" onclick={() => session.clear(true)}><Trash2 />Clear</Button>
        </div>
        <div class="sep"></div>
        <MaterialPicker materials={session.materials} bind:value={session.material}
                        onpick={() => { if (session.tool !== 'brush' && session.tool !== 'fill') session.tool = 'brush'; }} />
        <div class="sep"></div>
        <LayersPanel {session} />
      </aside>

      <section class="stage">
        <div class="panel">
          <div class="phead">
            <span class="ptitle">Painting</span>
            <span class="faint tnum">{session.width}×{session.height}</span>
          </div>
          <PaintCanvas {session} {active} />
        </div>
        <div class="panel">
          <div class="phead">
            <span class="ptitle">Landscape</span>
            {#if session.rendering && !session.refining}<Spinner size={12} />{/if}
            <span class="spacer"></span>
            <Segmented size="sm" bind:value={session.show} items={[
              { value: 'live' as const, label: 'Live', title: 'GauGAN frame, updated as you paint' },
              { value: 'refined' as const, label: 'Photo', title: 'Refined photo of this painting', disabled: !session.refined },
            ]} />
          </div>
          <ResultPanel {session} />
        </div>
      </section>

      <aside class="right">
        {#if avail === 'partial'}
          <div class="notice">
            <p>Live preview is ready. Install the refine models for photo quality, seeds and text guidance.</p>
            <Button size="sm" onclick={() => (installDialog = true)}>Add refine models</Button>
          </div>
        {/if}
        <GeneratePanel {session} />
      </aside>
    </div>
  {/if}
</div>

<OpenDialog bind:open={openDialog} current={session.doc.id} onopen={openDoc} />

<Modal bind:open={newDialog} title="New painting" subtitle="Canvas sizes are native to the live model" width={460}>
  <div class="sizes">
    {#each st?.canvas.sizes ?? [] as s (s.id)}
      <button class="size" class:on={newSize === s.id} onclick={() => (newSize = s.id)}>
        <span class="shape" style:aspect-ratio="{s.width} / {s.height}"></span>
        <span>{s.label}</span>
        <span class="faint tnum">{s.width}×{s.height}</span>
      </button>
    {/each}
  </div>
  {#snippet footer()}
    <Button variant="ghost" onclick={() => (newDialog = false)}>Cancel</Button>
    <Button variant="primary" onclick={createNew}>Create</Button>
  {/snippet}
</Modal>

{#if st}
  <Modal bind:open={installDialog} title="Refine models" width={600}>
    <InstallPanel {session} />
  </Modal>
{/if}

<style>
  .pb { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  .bar {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 52px;
    padding: 0 12px 0 8px;
    border-bottom: 1px solid var(--line-0);
    flex-shrink: 0;
  }
  .bar :global(.btn svg) { width: 14px; height: 14px; }
  .titles { display: flex; flex-direction: column; min-width: 0; line-height: 1.2; }
  .titles .eyebrow { font-size: 10px; }
  .doc {
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: 360px;
    padding: 0;
    border: 0;
    background: none;
    font-weight: 600;
    font-size: var(--fs-md);
    color: var(--text-0);
    cursor: text;
    text-align: left;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warn); flex-shrink: 0; }
  .gpu {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-left: 6px;
    padding: 4px 10px;
    border-radius: var(--r-full);
    background: var(--bg-2);
    font-size: var(--fs-xs);
    color: var(--text-2);
    white-space: nowrap;
  }
  .led { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); }
  .led.on { background: var(--ok); }
  .led.busy { background: var(--warn); }
  .center { flex: 1; display: grid; place-items: center; min-height: 0; padding: 24px; }
  .center.scroll { overflow-y: auto; place-items: start center; }
  .body { flex: 1; min-height: 0; display: grid; grid-template-columns: 236px 1fr 276px; }
  .left, .right {
    min-height: 0;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .left { border-right: 1px solid var(--line-0); }
  .right { border-left: 1px solid var(--line-0); }
  .tools { display: flex; gap: 2px; padding: 3px; border-radius: var(--r-md); background: var(--bg-2); justify-content: space-between; }
  .row { display: flex; align-items: center; gap: 2px; }
  .row :global(.btn svg) { width: 14px; height: 14px; }
  .sep { height: 1px; background: var(--line-0); margin: 2px 0; flex-shrink: 0; }
  .stage { min-width: 0; min-height: 0; display: grid; grid-template-columns: 1fr 1fr; }
  .panel { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .panel + .panel { border-left: 1px solid var(--line-0); }
  .phead { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 12px; border-bottom: 1px solid var(--line-0); flex-shrink: 0; font-size: var(--fs-sm); }
  .ptitle { font-weight: 600; color: var(--text-0); }
  .notice { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; border-radius: var(--r-md); background: var(--accent-ghost); font-size: var(--fs-sm); }
  .sizes { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .size {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding: 12px 6px;
    border: 1px solid var(--line-1);
    border-radius: var(--r-md);
    background: var(--bg-0);
    cursor: pointer;
    font-size: var(--fs-sm);
  }
  .size.on { border-color: var(--select-line); background: var(--select-bg); }
  .shape { width: 52px; max-height: 64px; border-radius: 3px; background: linear-gradient(#8ec5f4 60%, #79c25a 60%); box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.12); }
  @media (max-width: 1280px) {
    .body { grid-template-columns: 220px 1fr 256px; }
    .stage { grid-template-columns: 1fr; grid-template-rows: 1fr 1fr; }
    .panel + .panel { border-left: 0; border-top: 1px solid var(--line-0); }
  }
</style>
