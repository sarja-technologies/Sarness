<script lang="ts">
  import { Check, ChevronDown, FolderOpen, FolderPlus, Folder, ShieldCheck, ShieldAlert, Shield } from '@lucide/svelte';
  import { confirmDialog, promptDialog } from '../../ui/dialogs.svelte';
  import Menu, { type MenuItem } from '../../ui/Menu.svelte';
  import { toastError } from '../../stores/toast.svelte';
  import { native } from '../native';
  import { workspaceTitle } from '../prompt';
  import { agentStore } from '../state.svelte';
  import { PERMISSION_HINT, PERMISSION_LABEL, type PermissionMode } from '../types';

  let { project, permission, locked, onproject, onpermission }: {
    project: string | null;
    permission: PermissionMode;
    locked: boolean; // a task is running: project cannot change underneath it
    onproject: (path: string) => void;
    onpermission: (m: PermissionMode) => void;
  } = $props();

  const shortPath = (p: string) => {
    const t = p.replace(/^\/home\/[^/]+/, '~').split('/');
    return t.length > 4 ? `${t[0]}/…/${t.slice(-2).join('/')}` : t.join('/');
  };
  let menu = $state<{ open: boolean; x: number; y: number; items: MenuItem[] }>({ open: false, x: 0, y: 0, items: [] });

  async function refresh() {
    try {
      agentStore.projects = await native.projects();
    } catch {
      /* not in the desktop app */
    }
  }
  $effect(() => {
    void refresh();
  });

  async function openFolder() {
    try {
      const p = await native.pickFolder('Open project folder');
      if (!p) return;
      agentStore.projects = await native.addProject(p);
      onproject(agentStore.projects[0].path);
    } catch (e) {
      toastError(e, 'Could not open the folder');
    }
  }
  async function newProject() {
    try {
      const name = await promptDialog('New project', '', { body: 'A new folder with this name is created in the location you pick next.', placeholder: 'my-website', confirmLabel: 'Choose location…' });
      if (!name?.trim()) return;
      const parent = await native.pickFolder('Where should the project folder be created?');
      if (!parent) return;
      const dir = await native.createProject(parent, name.trim());
      agentStore.projects = await native.projects();
      onproject(dir);
    } catch (e) {
      toastError(e, 'Could not create the project');
    }
  }
  function openProjectMenu(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menu = {
      open: true,
      x: r.left,
      y: Math.max(8, r.top - 12 - Math.min(agentStore.projects.length + 3, 9) * 36),
      items: [
        ...agentStore.projects.slice(0, 8).map((p) => ({ label: p.name, hint: shortPath(p.path), icon: p.path === project ? Check : Folder, run: () => onproject(p.path) })),
        ...(agentStore.projects.length ? [{ separator: true }] : []),
        { label: 'Open folder…', icon: FolderOpen, run: openFolder },
        { label: 'New project…', icon: FolderPlus, run: newProject },
      ],
    };
  }
  async function pickPermission(m: PermissionMode) {
    if (m === 'full_access') {
      const ok = await confirmDialog('Enable Full Access?', 'The agent will run commands and edit files without asking each time, including installs and paths outside this project. Destructive, remote and privileged actions still ask. Only enable this for a task and project you trust, and never with untrusted repositories.', { confirmLabel: 'Enable Full Access', danger: true });
      if (!ok) return;
    }
    onpermission(m);
  }
  function openPermMenu(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menu = {
      open: true,
      x: r.left,
      y: Math.max(8, r.top - 12 - 3 * 36),
      items: (['ask', 'workspace_auto', 'full_access'] as PermissionMode[]).map((m) => ({ label: PERMISSION_LABEL[m], icon: m === permission ? Check : undefined, hint: m === 'full_access' ? 'warning' : undefined, run: () => void pickPermission(m) })),
    };
  }
  const PermIcon = $derived(permission === 'full_access' ? ShieldAlert : permission === 'ask' ? Shield : ShieldCheck);
</script>

<div class="bar">
  <button type="button" class="pill proj" disabled={locked} onclick={openProjectMenu} aria-haspopup="menu" title={project ?? 'No project folder selected'}>
    <Folder />
    {#if project}<span class="truncate">{workspaceTitle(project)}</span><span class="path truncate">{project.replace(/^\/home\/[^/]+/, '~')}</span>{:else}<span class="need">Choose a project folder</span>{/if}
    <ChevronDown />
  </button>
  <button type="button" class="pill perm" class:danger={permission === 'full_access'} disabled={locked} onclick={openPermMenu} aria-haspopup="menu" title={PERMISSION_HINT[permission]}>
    <PermIcon /><span>{PERMISSION_LABEL[permission]}</span><ChevronDown />
  </button>
</div>
<Menu bind:items={menu.items} bind:x={menu.x} bind:y={menu.y} bind:open={menu.open} />

<style>
  .bar { display: flex; align-items: center; gap: 6px; padding: 0 2px 4px; min-width: 0; }
  .pill { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 9px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-1); font-size: var(--fs-xs); cursor: pointer; min-width: 0; max-width: 100%; }
  .pill:hover:not(:disabled) { background: var(--select-bg); }
  .pill:disabled { opacity: 0.6; cursor: default; }
  .pill :global(svg) { width: 13px; height: 13px; flex-shrink: 0; color: var(--accent); }
  .pill :global(svg:last-child) { color: var(--text-3); width: 12px; height: 12px; }
  .proj { flex: 0 1 auto; }
  .path { color: var(--text-3); max-width: 240px; }
  .need { color: var(--warn); font-weight: 600; }
  .perm.danger { border-color: var(--err); color: var(--err); }
  .perm.danger :global(svg:first-child) { color: var(--err); }
  @container (max-width: 560px) { .path { display: none; } }
</style>
