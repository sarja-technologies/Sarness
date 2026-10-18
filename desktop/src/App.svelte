<script lang="ts">
  import { onMount } from 'svelte';
  import Sidebar from './lib/shell/Sidebar.svelte';
  import StatusBar from './lib/shell/StatusBar.svelte';
  import Onboarding from './lib/shell/Onboarding.svelte';
  import Toaster from './lib/ui/Toaster.svelte';
  import DialogHost from './lib/ui/DialogHost.svelte';
  import Spinner from './lib/ui/Spinner.svelte';
  import Workspace from './lib/views/Workspace.svelte';
  import Tiler from './lib/workspace/Tiler.svelte';
  import { applyTheme } from './lib/stores/theme';
  import { focusPane, startNewConversation, toggleMaximize, ws } from './lib/stores/panes.svelte';
  import { syncServerConversations } from './lib/stores/sessions.svelte';
  import Library from './lib/views/Library.svelte';
  import Models from './lib/views/Models.svelte';
  import Settings from './lib/views/Settings.svelte';
  import { conn, initConnection } from './lib/stores/connection.svelte';
  import { prefs, persistPrefs, type View } from './lib/stores/prefs.svelte';
  import { loadJobs, wireJobEvents } from './lib/stores/jobs.svelte';
  import { loadModels, wireModelEvents } from './lib/stores/models.svelte';
  import { loadProjects, wireProjectEvents } from './lib/stores/projects.svelte';

  const order: View[] = ['workspace', 'library', 'models', 'system', 'settings'];
  let visited = $state<Record<string, boolean>>({ [prefs.view]: true });
  let loadedFor = '';

  $effect(() => {
    visited[prefs.view] = true;
  });

  // (Re)load engine data whenever we come online with a (new) server.
  $effect(() => {
    const key = `${conn.state}:${conn.activeId}`;
    if (conn.state === 'online' && loadedFor !== key) {
      loadedFor = key;
      Promise.allSettled([loadJobs(), loadModels(), loadProjects(), syncServerConversations()]);
    }
  });

  onMount(() => {
    applyTheme(prefs.theme);
    wireJobEvents();
    wireModelEvents();
    wireProjectEvents();
    initConnection();
  });

  function keys(e: KeyboardEvent) {
    const n = Number(e.key);
    if (e.altKey && !e.ctrlKey && !e.metaKey && n >= 1 && n <= order.length) {
      prefs.view = order[n - 1];
      persistPrefs();
      e.preventDefault();
      return;
    }
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    // Ctrl+1..4 pick the composer mode (handled by the focused conversation); Ctrl+Shift+1..4 focus a pane.
    if (e.shiftKey && !e.altKey && ['!', '@', '#', '$'].includes(e.key)) {
      const p = ws.panes[['!', '@', '#', '$'].indexOf(e.key)];
      if (p) {
        prefs.view = 'workspace';
        ws.minimized = ws.minimized.filter((i) => i !== p.id);
        if (ws.maximized) ws.maximized = p.id;
        focusPane(p.id);
        e.preventDefault();
      }
    } else if (k === 'm' && e.shiftKey && ws.focus) {
      toggleMaximize(ws.focus);
      e.preventDefault();
    } else if (k === 'o' && e.shiftKey) {
      startNewConversation();
      prefs.view = 'workspace';
      persistPrefs();
      e.preventDefault();
    } else if (k === 'b' && !e.shiftKey) {
      prefs.sidebarCollapsed = !prefs.sidebarCollapsed;
      persistPrefs();
      e.preventDefault();
    }
  }
</script>

<svelte:window onkeydown={keys} />

{#if conn.state === 'booting'}
  <div class="boot"><Spinner size={22} /></div>
{:else if conn.state === 'unconfigured'}
  <Onboarding />
{:else}
  <div class="shell" class:collapsed={prefs.sidebarCollapsed}>
    <Sidebar />
    <main>
      <section class="view" class:hidden={prefs.view !== 'workspace'}><Tiler /></section>
      {#if visited.system}<section class="view" class:hidden={prefs.view !== 'system'}><Workspace /></section>{/if}
      {#if visited.library}<section class="view" class:hidden={prefs.view !== 'library'}><Library active={prefs.view === 'library'} /></section>{/if}
      {#if visited.models}<section class="view" class:hidden={prefs.view !== 'models'}><Models active={prefs.view === 'models'} /></section>{/if}
      {#if visited.settings}<section class="view" class:hidden={prefs.view !== 'settings'}><Settings /></section>{/if}
    </main>
    <StatusBar />
  </div>
{/if}

<Toaster />
<DialogHost />

<style>
  .boot { height: 100%; display: grid; place-items: center; }
  .shell {
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr;
    grid-template-rows: 1fr var(--statusbar-h);
    height: 100%;
    transition: grid-template-columns var(--t-3) var(--ease);
  }
  .shell.collapsed { grid-template-columns: var(--sidebar-w-collapsed) 1fr; }
  main {
    position: relative;
    min-width: 0;
    min-height: 0;
    background: var(--bg-0);
    border-top-left-radius: var(--r-xl);
    border-left: 1px solid var(--line-0);
    border-top: 1px solid var(--line-0);
    overflow: hidden;
  }
  .view { position: absolute; inset: 0; display: flex; flex-direction: column; animation: fade-in var(--t-2) var(--ease); }
  .view.hidden { display: none; }
</style>
