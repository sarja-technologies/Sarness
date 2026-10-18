<script lang="ts">
  import { Boxes, ChevronsUpDown, FolderPlus, Library, PanelLeftClose, PanelLeftOpen, Plus, Search, Server, Settings, SquarePen } from '@lucide/svelte';
  import { api, qs } from '../api/client';
  import type { Conversation } from '../api/types';
  import logo from '../assets/logo.svg';
  import { conn } from '../stores/connection.svelte';
  import { jobs } from '../stores/jobs.svelte';
  import { openSession, startNewConversation, ws } from '../stores/panes.svelte';
  import { prefs, persistPrefs, type View } from '../stores/prefs.svelte';
  import { createProject, currentFolder, currentProject, folderTree, projects, selectProject } from '../stores/projects.svelte';
  import { store } from '../stores/sessions.svelte';
  import { groupByRecency, isEmpty, matchesQuery, type Session } from '../stores/sessions';
  import { toastError } from '../stores/toast.svelte';
  import { promptDialog } from '../ui/dialogs.svelte';
  import Menu, { type MenuItem } from '../ui/Menu.svelte';
  import { tooltip } from '../ui/tooltip';
  import { deleteSession, renameSession } from '../workspace/sessionActions';
  import { MODE_LABEL } from '../workspace/params';

  const collapsed = $derived(prefs.sidebarCollapsed);
  const links: { id: View; label: string; icon: any; key: string }[] = [
    { id: 'library', label: 'Library', icon: Library, key: 'Alt+2' },
    { id: 'models', label: 'Models', icon: Boxes, key: 'Alt+3' },
    { id: 'system', label: 'Server', icon: Server, key: 'Alt+4' },
    { id: 'settings', label: 'Settings', icon: Settings, key: 'Alt+5' },
  ];

  function go(v: View) {
    prefs.view = v;
    persistPrefs();
  }
  function newConversation() {
    startNewConversation();
    go('workspace');
  }
  function open(s: Session) {
    openSession(s.id);
    go('workspace');
  }

  // ---- history + search -------------------------------------------------
  let q = $state('');
  let hits = $state(new Set<string>());
  let timer: ReturnType<typeof setTimeout>;
  $effect(() => {
    const term = q.trim();
    clearTimeout(timer);
    if (!term || conn.state !== 'online') {
      hits = new Set();
      return;
    }
    timer = setTimeout(async () => {
      try {
        hits = new Set((await api.get<Conversation[]>(`/v1/chat/conversations${qs({ q: term })}`)).map((c) => c.id));
      } catch {
        hits = new Set();
      }
    }, 250);
  });
  const visible = $derived(store.list.filter((s) => !isEmpty(s) && matchesQuery(s, q, hits)));
  const groups = $derived(groupByRecency(visible));
  const activeId = $derived(ws.panes.find((p) => p.id === ws.focus)?.sessionId);
  const busy = (s: Session) => s.entries.some((e) => {
    const j = jobs.byId[e.jobId];
    return j && (j.status === 'queued' || j.status === 'running');
  });

  function context(e: MouseEvent, s: Session) {
    e.preventDefault();
    menu = { open: true, x: e.clientX, y: e.clientY, items: [
      { label: 'Rename', run: () => renameSession(s) },
      { label: 'Delete', danger: true, run: () => deleteSession(s) },
    ] };
  }

  let menu = $state({ open: false, x: 0, y: 0, items: [] as MenuItem[] });
  function openProjects(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const items: MenuItem[] = [
      { label: 'All work (no project)', run: () => selectProject(null) },
      { separator: true },
    ];
    for (const p of projects.list) {
      items.push({ label: p.name, run: () => selectProject(p.id) });
      for (const f of flatten(folderTree(p))) {
        items.push({ label: `${'   '.repeat(f.depth + 1)}${f.name}`, run: () => selectProject(p.id, f.id) });
      }
    }
    items.push({ separator: true }, { label: 'New project…', icon: FolderPlus, run: newProject });
    menu = { open: true, x: r.left + 6, y: r.bottom + 4, items };
  }
  function flatten(nodes: any[]): any[] {
    return nodes.flatMap((n) => [n, ...flatten(n.children)]);
  }
  async function newProject() {
    const name = await promptDialog('New project', '', { placeholder: 'Project name', confirmLabel: 'Create' });
    if (!name) return;
    try {
      const p = await createProject(name);
      selectProject(p.id);
    } catch (e) {
      toastError(e, 'Could not create project');
    }
  }
  const projectLabel = $derived(currentProject()?.name ?? 'All work');
  const folderLabel = $derived(currentFolder()?.name ?? null);
</script>

<aside class="sidebar" class:collapsed aria-label="Sidebar">
  <div class="brand">
    <img src={logo} alt="" width="28" height="28" />
    {#if !collapsed}<span class="word">Sarness</span>{/if}
  </div>

  <button class="new" onclick={newConversation} use:tooltip={collapsed ? 'New conversation' : null}>
    <SquarePen />{#if !collapsed}<span>New conversation</span>{/if}
  </button>

  {#if !collapsed}
    <label class="search"><Search /><input bind:value={q} placeholder="Search conversations" aria-label="Search conversations" /></label>

    <button class="project" onclick={openProjects}>
      <span class="dot" style:background={currentProject() ? 'var(--accent-hi)' : 'var(--text-3)'}></span>
      <span class="ptext">
        <span class="pname truncate">{projectLabel}</span>
        {#if folderLabel}<span class="pfolder truncate">{folderLabel}</span>{/if}
      </span>
      <ChevronsUpDown />
    </button>

    <nav class="history" aria-label="Conversation history">
      {#each groups as g (g.label)}
        <div class="sect">{g.label}</div>
        {#each g.items as s (s.id)}
          <button class="item" class:active={s.id === activeId} onclick={() => open(s)} oncontextmenu={(e) => context(e, s)} title={s.title}>
            <span class="t truncate">{s.title}</span>
            {#if busy(s)}<span class="run" title="Generating"></span>{:else}<span class="m">{MODE_LABEL[s.mode]}</span>{/if}
          </button>
        {/each}
      {/each}
      {#if !visible.length}<p class="none faint">{q.trim() ? 'No matches' : 'Your conversations appear here'}</p>{/if}
    </nav>
  {:else}
    <div class="grow"></div>
  {/if}

  <div class="bottom">
    {#each links as l (l.id)}
      <button class="link" class:active={prefs.view === l.id} onclick={() => go(l.id)} use:tooltip={collapsed ? `${l.label}  ·  ${l.key}` : null}>
        <l.icon />{#if !collapsed}<span>{l.label}</span>{/if}
      </button>
    {/each}
    <button class="link subtle" onclick={() => { prefs.sidebarCollapsed = !collapsed; persistPrefs(); }} use:tooltip={collapsed ? 'Expand sidebar  ·  Ctrl+B' : null}>
      {#if collapsed}<PanelLeftOpen />{:else}<PanelLeftClose /><span>Collapse</span>{/if}
    </button>
  </div>
</aside>

<Menu bind:open={menu.open} bind:x={menu.x} bind:y={menu.y} bind:items={menu.items} />

<style>
  .sidebar { grid-row: 1 / span 2; display: flex; flex-direction: column; gap: 8px; padding: 12px 10px 10px; background: var(--bg-1); min-width: 0; overflow: hidden; }
  .brand { display: flex; align-items: center; gap: 10px; padding: 4px 6px 6px; height: 40px; }
  .brand img { flex-shrink: 0; border-radius: 8px; }
  .word { font-weight: 650; font-size: 16px; letter-spacing: -0.02em; color: var(--text-0); }
  .new { display: flex; align-items: center; gap: 10px; height: 38px; padding: 0 10px; border-radius: var(--r-md); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-0); font-weight: 600; font-size: var(--fs-md); cursor: pointer; transition: border-color var(--t-1), background var(--t-1); }
  .new:hover { border-color: var(--accent); }
  .new :global(svg) { width: 16px; height: 16px; color: var(--accent); flex-shrink: 0; }
  .search { display: flex; align-items: center; gap: 8px; height: 32px; padding: 0 10px; border-radius: var(--r-md); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-3); }
  .search :global(svg) { width: 14px; height: 14px; flex-shrink: 0; }
  .search input { flex: 1; min-width: 0; border: 0; background: none; color: var(--text-0); outline: none; font-size: var(--fs-sm); }
  .search:focus-within { border-color: var(--select-line); }
  .project {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-bottom: 8px;
    padding: 8px 9px;
    border-radius: var(--r-md);
    border: 1px solid var(--line-1);
    background: var(--bg-2);
    color: var(--text-1);
    cursor: pointer;
    text-align: left;
    min-height: 40px;
    transition: border-color var(--t-1), background var(--t-1);
  }
  .project:hover { border-color: var(--line-2); background: var(--bg-3); }
  .project :global(svg) { width: 14px; height: 14px; color: var(--text-3); flex-shrink: 0; }
  .dot { width: 8px; height: 8px; border-radius: 3px; flex-shrink: 0; margin-left: 2px; }
  .ptext { display: flex; flex-direction: column; flex: 1; min-width: 0; line-height: 1.25; }
  .pname { font-weight: 600; font-size: var(--fs-md); color: var(--text-0); }
  .pfolder { font-size: var(--fs-xs); color: var(--text-3); }
  .history { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; margin: 4px -4px 0; padding: 0 4px; }
  .sect { padding: 10px 8px 4px; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-3); }
  .item { display: flex; align-items: center; gap: 8px; width: 100%; height: 34px; padding: 0 10px; border: 0; border-radius: var(--r-md); background: none; color: var(--text-1); cursor: pointer; text-align: left; font-size: var(--fs-md); transition: background var(--t-1); }
  .item:hover { background: rgba(var(--fg-rgb), 0.05); color: var(--text-0); }
  .item.active { background: var(--select-bg); color: var(--text-0); }
  .t { flex: 1; min-width: 0; }
  .m { font-size: 10.5px; color: var(--text-3); }
  .run { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse-ring 1.8s infinite; flex-shrink: 0; }
  .none { padding: 12px 10px; font-size: var(--fs-sm); }
  .grow { flex: 1; }
  .bottom { display: flex; flex-direction: column; gap: 1px; padding-top: 6px; border-top: 1px solid var(--line-0); }
  .link { position: relative; display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 10px; border: 0; border-radius: var(--r-md); background: none; color: var(--text-2); cursor: pointer; font-size: var(--fs-md); text-align: left; }
  .link :global(svg) { width: 16px; height: 16px; flex-shrink: 0; }
  .link:hover { background: rgba(var(--fg-rgb), 0.05); color: var(--text-0); }
  .link.active { background: var(--select-bg); color: var(--text-0); }
  .link.active :global(svg) { color: var(--accent); }
  .subtle { color: var(--text-3); }
  .collapsed .new, .collapsed .link { justify-content: center; padding: 0; }
  .collapsed .brand { justify-content: center; padding: 4px 0; }
</style>
