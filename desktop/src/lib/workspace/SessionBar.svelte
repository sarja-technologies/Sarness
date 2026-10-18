<script lang="ts">
  import { Columns2, Ellipsis, Grid2x2, GripVertical, LayoutPanelLeft, Maximize2, Minimize2, Minus, PanelLeft, Plus, Rows2, Square, X } from '@lucide/svelte';
  import { MAX_PANES, addPane, closePane, setLayout, toggleMaximize, toggleMinimize, ws } from '../stores/panes.svelte';
  import type { Session } from '../stores/sessions';
  import IconButton from '../ui/IconButton.svelte';
  import Menu, { type MenuItem } from '../ui/Menu.svelte';
  import { deleteSession, exportSession, renameSession } from './sessionActions';

  /** Slim header for one conversation. Pane controls appear only in the optional multi-conversation layout. */
  let { session, paneId, multi, onremoved, ondragstart }: { session: Session; paneId: string; multi: boolean; onremoved: () => void; ondragstart?: (e: DragEvent) => void } = $props();

  let menu = $state({ open: false, x: 0, y: 0, items: [] as MenuItem[] });
  const maximized = $derived(ws.maximized === paneId);
  const canClose = $derived(ws.panes.length > 1);
  const canMin = $derived(ws.panes.length - ws.minimized.length > 1);

  function actions(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menu = { open: true, x: Math.max(8, r.right - 200), y: r.bottom + 4, items: [
      { label: 'Rename', run: () => renameSession(session) },
      { label: 'Export as Markdown', run: () => exportSession(session, 'md') },
      { label: 'Export as JSON', run: () => exportSession(session, 'json') },
      { separator: true },
      { label: 'Delete conversation', danger: true, run: async () => { if (await deleteSession(session)) onremoved(); } },
    ] };
  }
  function layoutMenu(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menu = { open: true, x: Math.max(8, r.right - 230), y: r.bottom + 4, items: [
      { label: 'One conversation', icon: Square, run: () => setLayout(1) },
      { label: 'Two side by side', icon: Columns2, run: () => setLayout(2, 'cols') },
      { label: 'Two stacked', icon: Rows2, run: () => setLayout(2, 'rows') },
      { label: 'Three conversations', icon: PanelLeft, run: () => setLayout(3) },
      { label: 'Four conversations (2×2)', icon: Grid2x2, run: () => setLayout(4) },
      { separator: true },
      { label: 'Add a conversation', icon: Plus, disabled: ws.panes.length >= MAX_PANES, run: () => addPane() },
    ] };
  }
</script>

<header class="bar" class:multi>
  {#if multi}
    <span class="grip" draggable="true" {ondragstart} title="Drag onto another conversation to swap places" role="presentation"><GripVertical /></span>
  {/if}
  <h2 class="title truncate" title={session.title}>{session.title}</h2>
  <span class="sp"></span>
  <IconButton label="Conversation actions" size="sm" onclick={actions}><Ellipsis /></IconButton>
  <IconButton label="Layout: one or several conversations side by side" size="sm" onclick={layoutMenu}><LayoutPanelLeft /></IconButton>
  {#if multi}
    <IconButton label="Minimise" size="sm" disabled={!canMin} onclick={() => toggleMinimize(paneId)}><Minus /></IconButton>
    <IconButton label={maximized ? 'Restore layout' : 'Maximise'} size="sm" onclick={() => toggleMaximize(paneId)}>{#if maximized}<Minimize2 />{:else}<Maximize2 />{/if}</IconButton>
    <IconButton label="Close this pane (conversation stays in history)" size="sm" disabled={!canClose} onclick={() => closePane(paneId)}><X /></IconButton>
  {/if}
</header>

<Menu bind:open={menu.open} bind:x={menu.x} bind:y={menu.y} bind:items={menu.items} />

<style>
  .bar { display: flex; align-items: center; gap: 4px; height: 44px; padding: 0 8px 0 18px; flex-shrink: 0; }
  .bar.multi { height: 36px; padding-left: 2px; background: var(--bg-1); border-bottom: 1px solid var(--line-0); }
  .title { font-size: var(--fs-md); font-weight: 600; min-width: 0; letter-spacing: 0; }
  .sp { flex: 1; }
  .grip { display: grid; place-items: center; width: 20px; height: 28px; color: var(--text-3); cursor: grab; }
  .grip :global(svg) { width: 14px; height: 14px; }
</style>
