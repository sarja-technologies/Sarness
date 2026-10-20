<script lang="ts">
  import { Clapperboard, Download, FileJson, Folder, FolderOpen, FolderPlus, Heart, Image as ImageIcon, LayoutGrid, Library as LibIcon, Play, Plus, Repeat, Search, Shuffle, Star, Tag, Trash2, Undo2, X, Layers, Grid3x3, CalendarDays, Boxes } from '@lucide/svelte';
  import { apiFor, qs } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import type { Collection, Generation, Job, Page } from '../api/types';
  import { conn, engineList, onEvent, profileName } from '../stores/connection.svelte';
  import { onGeneration } from '../stores/jobs.svelte';
  import { modelById } from '../stores/models.svelte';
  import { prefs, persistPrefs } from '../stores/prefs.svelte';
  import { createFolder, deleteFolder, deleteProject, folderPath, folderTree, projects, renameFolder, renameProject, createProject } from '../stores/projects.svelte';
  import { toast, toastError } from '../stores/toast.svelte';
  import { exportAs, exportMetadata, openInFolder, toggleFavorite } from '../studio/actions';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Empty from '../ui/Empty.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Menu, { type MenuItem } from '../ui/Menu.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Splitter from '../ui/Splitter.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import { confirmDialog, promptDialog } from '../ui/dialogs.svelte';
  import { ago, bytes, dateTime, duration } from '../util/format';

  let { active }: { active: boolean } = $props();

  // Each server keeps its own library; with several connected, one is shown at a time.
  let server = $state<string | null>(null);
  const srv = $derived(server && conn.engines[server] ? server : conn.activeId);
  const api = $derived(apiFor(srv));
  const servers = $derived(engineList().map((e) => ({ value: e.id as string | null, label: profileName(e.id), hint: e.state === 'online' ? '' : 'offline' })));
  // Projects and folders live on the primary server.
  const onPrimary = $derived(srv === conn.activeId);
  const tag = (g: Generation): Generation => ({ ...g, engine: srv ?? undefined });

  type Filter = { type: '' | 'image' | 'video'; favorite: boolean; trash: boolean; project_id: string | null; folder_id: string | null; collection_id: string | null; tag: string | null; model_id: string | null; since: string; q: string };
  let f = $state<Filter>({ type: '', favorite: false, trash: false, project_id: null, folder_id: null, collection_id: null, tag: null, model_id: null, since: '', q: '' });
  let items = $state<Generation[]>([]);
  let cursor = $state<number | null>(null);
  let loading = $state(false);
  let selected = $state<Set<string>>(new Set());
  let anchor: number | null = null;
  let detail = $state<Generation | null>(null);
  let collections = $state<Collection[]>([]);
  let tags = $state<{ tag: string; count: number }[]>([]);
  let usedModels = $state<{ model_id: string; type: string; count: number }[]>([]);
  let navW = $state(244);
  let detailW = $state(380);
  let menu = $state({ open: false, x: 0, y: 0, items: [] as MenuItem[] });
  let loadedOnce = false;
  let shownFor: string | null = null;
  let newTag = $state('');

  const sinceTs = $derived(f.since ? Date.now() / 1000 - Number(f.since) * 86400 : null);

  async function load(reset = true) {
    if (loading) return;
    loading = true;
    try {
      const page = await api.get<Page<Generation>>(`/v1/generations${qs({
        type: f.type, favorite: f.favorite ? true : undefined, trash: f.trash || undefined, project_id: f.project_id, folder_id: f.folder_id,
        collection_id: f.collection_id, tag: f.tag, model_id: f.model_id, q: f.q, since: sinceTs, limit: 80, before: reset ? undefined : cursor,
      })}`);
      const got = page.items.map(tag);
      items = reset ? got : [...items, ...got.filter((g) => !items.some((x) => x.id === g.id))];
      cursor = page.next_cursor;
      if (reset) selected = new Set();
    } catch (e) {
      toastError(e, 'Could not load the library');
    } finally {
      loading = false;
    }
  }

  async function loadSide() {
    try {
      [collections, tags, usedModels] = await Promise.all([
        api.get<Collection[]>('/v1/collections'), api.get('/v1/tags'), api.get('/v1/library/models'),
      ]);
    } catch {
      /* non-fatal */
    }
  }

  $effect(() => {
    if (active && !loadedOnce) {
      loadedOnce = true;
      shownFor = srv;
      load();
      loadSide();
    }
  });

  let timer: ReturnType<typeof setTimeout>;
  $effect(() => {
    const id = srv;
    if (!loadedOnce || id === shownFor) return;
    shownFor = id;
    detail = null;
    if (id !== conn.activeId) f = { ...f, project_id: null, folder_id: null };
    load();
    loadSide();
  });
  $effect(() => {
    JSON.stringify(f);
    clearTimeout(timer);
    if (loadedOnce) timer = setTimeout(() => load(), 180);
  });

  onGeneration((g) => {
    if ((g.engine ?? conn.activeId) !== srv) return;
    if (!loadedOnce || f.trash || f.favorite || f.collection_id || f.tag || f.q) return;
    if ((f.type && g.type !== f.type) || (f.project_id && g.project_id !== f.project_id) || (f.folder_id && g.folder_id !== f.folder_id)) return;
    items = [g, ...items];
  });
  onEvent('generations.changed', (e) => loadedOnce && (e.engine ?? conn.activeId) === srv && load());

  function setScope(patch: Partial<Filter>) {
    f = { ...f, project_id: null, folder_id: null, collection_id: null, tag: null, trash: false, favorite: false, ...patch };
  }

  function click(e: MouseEvent, g: Generation, i: number) {
    if (e.shiftKey && anchor !== null) {
      const [a, b] = [Math.min(anchor, i), Math.max(anchor, i)];
      const s = new Set(selected);
      for (let k = a; k <= b; k++) s.add(items[k].id);
      selected = s;
    } else if (e.ctrlKey || e.metaKey) {
      const s = new Set(selected);
      s.has(g.id) ? s.delete(g.id) : s.add(g.id);
      selected = s;
      anchor = i;
    } else {
      selected = new Set([g.id]);
      anchor = i;
      openDetail(g);
    }
  }

  async function openDetail(g: Generation) {
    detail = g;
    try {
      detail = tag(await api.get<Generation>(`/v1/generations/${g.id}`));
    } catch {
      /* keep the summary */
    }
  }

  function scroll(e: Event) {
    const el = e.currentTarget as HTMLElement;
    if (cursor && !loading && el.scrollTop + el.clientHeight > el.scrollHeight - 600) load(false);
  }

  async function bulk(action: string, extra: Record<string, unknown> = {}) {
    const ids = [...selected];
    if (!ids.length) return;
    try {
      await api.post('/v1/generations/bulk', { ids, action, ...extra });
      toast('success', `${ids.length} item${ids.length > 1 ? 's' : ''} updated`);
      load();
      loadSide();
    } catch (e) {
      toastError(e);
    }
  }

  function moveMenu(e: MouseEvent) {
    const items: MenuItem[] = [{ label: 'Unfiled', run: () => bulk('move', { project_id: null, folder_id: null }) }];
    for (const p of projects.list) {
      items.push({ label: p.name, icon: FolderOpen, run: () => bulk('move', { project_id: p.id, folder_id: null }) });
      const walk = (nodes: any[]) => nodes.forEach((n) => { items.push({ label: `${'   '.repeat(n.depth + 1)}${n.name}`, run: () => bulk('move', { project_id: p.id, folder_id: n.id }) }); walk(n.children); });
      walk(folderTree(p));
    }
    menu = { open: true, x: e.clientX, y: e.clientY, items };
  }

  function collectionMenu(e: MouseEvent) {
    menu = {
      open: true, x: e.clientX, y: e.clientY, items: [
        ...collections.map((c) => ({ label: c.name, icon: Layers, run: () => addToCollection(c.id) })),
        { separator: true },
        { label: 'New collection…', icon: Plus, run: async () => { const n = await promptDialog('New collection', '', { confirmLabel: 'Create' }); if (n) { const c = await api.post<Collection>('/v1/collections', { name: n }); await addToCollection(c.id); } } },
      ],
    };
  }

  async function addToCollection(id: string) {
    await api.post(`/v1/collections/${id}/items`, { generation_ids: [...selected] });
    toast('success', 'Added to collection');
    loadSide();
  }

  async function tagSelected() {
    const t = await promptDialog('Add tag', '', { placeholder: 'e.g. moodboard', confirmLabel: 'Add' });
    if (t) bulk('tag', { tag: t });
  }

  async function saveTags(g: Generation, list: string[]) {
    const out = tag(await api.patch<Generation>(`/v1/generations/${g.id}`, { tags: list }));
    detail = { ...detail!, tags: out.tags };
    loadSide();
  }

  async function rerun(g: Generation, newSeed: boolean) {
    try {
      await api.post<Job>(`/v1/generations/${g.id}/rerun`, { new_seed: newSeed });
      toast('success', 'Queued', 'The result will appear in the library and the studio.');
    } catch (e) {
      toastError(e, 'Rerun failed');
    }
  }

  async function permanentDelete() {
    if (!(await confirmDialog(`Permanently delete ${selected.size} item(s)?`, 'Files are removed from the server. This cannot be undone.', { danger: true, confirmLabel: 'Delete forever' }))) return;
    for (const id of selected) await api.del(`/v1/generations/${id}?permanent=true`);
    load();
  }

  function projectMenu(e: MouseEvent, p: (typeof projects.list)[number], folderId: string | null = null) {
    e.preventDefault();
    menu = {
      open: true, x: e.clientX, y: e.clientY, items: [
        { label: 'New folder…', icon: FolderPlus, run: async () => { const n = await promptDialog('New folder', '', { confirmLabel: 'Create' }); if (n) await createFolder(p.id, n, folderId); } },
        { label: 'Rename…', run: async () => { const cur = folderId ? p.folders.find((x) => x.id === folderId)?.name : p.name; const n = await promptDialog('Rename', cur ?? ''); if (n) folderId ? renameFolder(folderId, n) : renameProject(p.id, n); } },
        { separator: true },
        { label: folderId ? 'Delete folder' : 'Delete project', icon: Trash2, danger: true, run: async () => {
          if (await confirmDialog(folderId ? 'Delete folder?' : `Delete project “${p.name}”?`, 'Generations and conversations inside are kept and become unfiled.', { danger: true, confirmLabel: 'Delete' })) {
            folderId ? await deleteFolder(folderId) : await deleteProject(p.id);
            setScope({});
          }
        } },
      ],
    };
  }

  function dragStart(e: DragEvent, g: Generation) {
    e.dataTransfer?.setData('application/x-sarness-file', g.file_id);
    e.dataTransfer?.setData('text/plain', g.prompt ?? '');
  }

  const meta = $derived((detail?.metadata ?? {}) as Record<string, any>);
  const settingsRows = $derived(detail ? [
    ['Model', modelById(detail.model_id)?.name ?? detail.model_id],
    ['Size', `${detail.width} × ${detail.height}`],
    ...(detail.type === 'video' ? [['Video', `${detail.frames} frames · ${detail.fps} fps · ${duration(detail.duration)}`]] : []),
    ['Seed', detail.seed],
    ['Steps', meta.steps], ['Guidance', meta.guidance], ['Scheduler', meta.scheduler], ['Mode', meta.mode],
    ['Strength', meta.denoise], ['LoRAs', (meta.loras ?? []).map((l: any) => `${l.name ?? l.id} (${l.strength})`).join(', ')],
    ['Memory mode', meta.memory_mode], ['Time', meta.elapsed_s ? duration(meta.elapsed_s) : null],
    ['Peak VRAM', meta.peak_vram_mb ? `${(meta.peak_vram_mb / 1024).toFixed(1)} GB` : null],
    ['File', detail.file ? `${detail.file.mime} · ${bytes(detail.file.size)}` : null],
    ['Created', dateTime(detail.created_at)],
    ['Location', folderPath(projects.list.find((p) => p.id === detail!.project_id) ?? null, detail.folder_id)],
  ].filter(([, v]) => v !== null && v !== undefined && v !== '') : []);
</script>

<div class="lib">
  <nav class="side" style="width:{navW}px">
    <div class="sh"><h1>Library</h1></div>
    {#if servers.length > 1}
      <div class="srv"><Select value={srv} options={servers} size="sm" onchange={(v) => (server = v)} /></div>
    {/if}
    <div class="group">
      <button class:on={!f.type && !f.favorite && !f.trash && !f.project_id && !f.collection_id && !f.tag} onclick={() => { f.type = ''; setScope({}); }}><LayoutGrid />Everything</button>
      <button class:on={f.type === 'image' && !f.trash} onclick={() => { f.type = 'image'; f.trash = false; }}><ImageIcon />Images</button>
      <button class:on={f.type === 'video' && !f.trash} onclick={() => { f.type = 'video'; f.trash = false; }}><Clapperboard />Videos</button>
      <button class:on={f.favorite} onclick={() => setScope({ favorite: true })}><Heart />Favorites</button>
      <button class:on={f.trash} onclick={() => setScope({ trash: true })}><Trash2 />Trash</button>
    </div>
    {#if onPrimary}
    <div class="label">Projects <IconButton label="New project" size="sm" onclick={async () => { const n = await promptDialog('New project', '', { confirmLabel: 'Create' }); if (n) createProject(n); }}><Plus /></IconButton></div>
    <div class="group">
      <button class:on={f.project_id === 'none'} onclick={() => setScope({ project_id: 'none' })}><Folder />Unfiled</button>
      {#each projects.list as p (p.id)}
        <button class:on={f.project_id === p.id && !f.folder_id} onclick={() => setScope({ project_id: p.id })} oncontextmenu={(e) => projectMenu(e, p)}>
          <FolderOpen /><span class="truncate">{p.name}</span><span class="n">{p.generation_count}</span>
        </button>
        {#snippet tree(nodes: any[])}
          {#each nodes as fo (fo.id)}
            <button class="sub" style="padding-left:{26 + fo.depth * 14}px" class:on={f.folder_id === fo.id} onclick={() => setScope({ project_id: p.id, folder_id: fo.id })} oncontextmenu={(e) => projectMenu(e, p, fo.id)}>
              <Folder /><span class="truncate">{fo.name}</span>
            </button>
            {@render tree(fo.children)}
          {/each}
        {/snippet}
        {@render tree(folderTree(p))}
      {/each}
    </div>
    {/if}
    {#if collections.length}
      <div class="label">Collections</div>
      <div class="group">
        {#each collections as c (c.id)}
          <button class:on={f.collection_id === c.id} onclick={() => setScope({ collection_id: c.id })}
            oncontextmenu={(e) => { e.preventDefault(); menu = { open: true, x: e.clientX, y: e.clientY, items: [
              { label: 'Rename…', run: async () => { const n = await promptDialog('Rename collection', c.name); if (n) { await api.patch(`/v1/collections/${c.id}`, { name: n }); loadSide(); } } },
              { label: 'Delete collection', icon: Trash2, danger: true, run: async () => { await api.del(`/v1/collections/${c.id}`); loadSide(); setScope({}); } },
            ] }; }}>
            <Layers /><span class="truncate">{c.name}</span><span class="n">{c.count}</span>
          </button>
        {/each}
      </div>
    {/if}
    {#if tags.length}
      <div class="label">Tags</div>
      <div class="tags">
        {#each tags.slice(0, 40) as t (t.tag)}
          <button class="tag" class:on={f.tag === t.tag} onclick={() => setScope({ tag: f.tag === t.tag ? null : t.tag })}>#{t.tag}<span>{t.count}</span></button>
        {/each}
      </div>
    {/if}
  </nav>
  <Splitter id="lib-nav" bind:size={navW} min={200} max={360} />

  <section class="main">
    <header>
      <div class="searchbox"><TextInput bind:value={f.q} placeholder="Search prompts and tags" size="sm">{#snippet icon()}<Search />{/snippet}</TextInput></div>
      <div class="fsel"><Select bind:value={f.model_id} size="sm" options={[{ value: null, label: 'All models' }, ...usedModels.map((m) => ({ value: m.model_id as string | null, label: modelById(m.model_id)?.name ?? m.model_id, hint: `${m.count} ${m.type}s` }))]} /></div>
      <div class="fsel narrow"><Select bind:value={f.since} size="sm" options={[{ value: '', label: 'Any time' }, { value: '1', label: 'Today' }, { value: '7', label: 'Last 7 days' }, { value: '30', label: 'Last 30 days' }, { value: '365', label: 'This year' }]} /></div>
      <div class="spacer"></div>
      <input class="zoom" type="range" min="120" max="360" step="10" bind:value={prefs.libraryThumb} onchange={persistPrefs} title="Thumbnail size" />
    </header>

    {#if selected.size > 1 || (selected.size === 1 && !detail)}
      <div class="bulk glass">
        <strong>{selected.size} selected</strong>
        <span class="spacer"></span>
        {#if f.trash}
          <Button size="sm" variant="ghost" onclick={() => bulk('restore')}><Undo2 />Restore</Button>
          <Button size="sm" variant="danger" onclick={permanentDelete}><Trash2 />Delete forever</Button>
        {:else}
          <Button size="sm" variant="ghost" onclick={() => bulk('favorite', { value: true })}><Star />Favorite</Button>
          <Button size="sm" variant="ghost" onclick={moveMenu}><FolderOpen />Move</Button>
          <Button size="sm" variant="ghost" onclick={collectionMenu}><Layers />Collect</Button>
          <Button size="sm" variant="ghost" onclick={tagSelected}><Tag />Tag</Button>
          <Button size="sm" variant="ghost" onclick={() => bulk('trash')}><Trash2 />Trash</Button>
        {/if}
        <IconButton label="Clear selection" size="sm" onclick={() => (selected = new Set())}><X /></IconButton>
      </div>
    {/if}

    <div class="grid-wrap" onscroll={scroll}>
      {#if !items.length && !loading}
        <Empty icon={f.trash ? Trash2 : LibIcon} title={f.trash ? 'Trash is empty' : 'Nothing here yet'} body={f.trash ? 'Deleted generations stay here until you remove them permanently.' : 'Generated images and videos are saved here automatically, with their full settings.'} />
      {/if}
      <div class="grid" style="--th:{prefs.libraryThumb}px">
        {#each items as g, i (g.id)}
          <button class="card" class:sel={selected.has(g.id)} onclick={(e) => click(e, g, i)} draggable="true" ondragstart={(e) => dragStart(e, g)}
            ondblclick={() => openDetail(g)}>
            <img src={mediaUrl(g.thumb_url, srv)} alt={g.prompt ?? ''} loading="lazy" decoding="async" />
            {#if g.type === 'video'}<span class="vb"><Play />{duration(g.duration)}</span>{/if}
            {#if g.favorite}<span class="fav"><Star /></span>{/if}
            <span class="cap">{g.prompt}</span>
          </button>
        {/each}
      </div>
      {#if loading}<div class="more"><Spinner /></div>{/if}
    </div>
  </section>

  {#if detail}
    <Splitter id="lib-detail" bind:size={detailW} min={300} max={620} side="right" />
    <aside class="detail" style="width:{detailW}px">
      <div class="dh">
        <span class="faint">{detail.type === 'video' ? 'Video' : 'Image'} · {ago(detail.created_at)}</span>
        <IconButton label="Close" size="sm" onclick={() => (detail = null)}><X /></IconButton>
      </div>
      <div class="dprev">
        {#if detail.type === 'video'}
          <!-- svelte-ignore a11y_media_has_caption -->
          <video src={mediaUrl(detail.url, srv)} controls loop autoplay muted></video>
        {:else}
          <img src={mediaUrl(detail.url, srv)} alt="" />
        {/if}
      </div>
      <div class="dacts">
        <IconButton label={detail.favorite ? 'Unfavorite' : 'Favorite'} active={!!detail.favorite} onclick={() => toggleFavorite(detail!)}><Star /></IconButton>
        <IconButton label="Rerun with a new seed" onclick={() => rerun(detail!, true)}><Shuffle /></IconButton>
        <IconButton label="Rerun exactly" onclick={() => rerun(detail!, false)}><Repeat /></IconButton>
        <IconButton label="Export as…" onclick={() => exportAs(detail!)}><Download /></IconButton>
        <IconButton label="Open in folder" onclick={() => openInFolder(detail!)}><FolderOpen /></IconButton>
        <IconButton label="Export metadata" onclick={() => exportMetadata(detail!)}><FileJson /></IconButton>
        <span class="spacer"></span>
        {#if detail.deleted_at}
          <Button size="sm" variant="ghost" onclick={async () => { await api.post('/v1/generations/bulk', { ids: [detail!.id], action: 'restore' }); detail = null; load(); }}><Undo2 />Restore</Button>
        {:else}
          <IconButton label="Move to trash" tone="danger" onclick={async () => { await api.del(`/v1/generations/${detail!.id}`); detail = null; }}><Trash2 /></IconButton>
        {/if}
      </div>
      <div class="dbody">
        <section>
          <div class="eyebrow">Prompt</div>
          <p class="prompt selectable">{detail.prompt || '—'}</p>
          {#if detail.negative_prompt}<div class="eyebrow">Negative</div><p class="neg selectable">{detail.negative_prompt}</p>{/if}
        </section>
        <section>
          <div class="eyebrow">Settings</div>
          <dl>
            {#each settingsRows as [k, v] (k)}<dt>{k}</dt><dd class="selectable">{v}</dd>{/each}
          </dl>
        </section>
        <section>
          <div class="eyebrow">Tags</div>
          <div class="dtags">
            {#each detail.tags ?? [] as t (t)}<span class="dtag">#{t}<button onclick={() => saveTags(detail!, (detail!.tags ?? []).filter((x) => x !== t))}><X /></button></span>{/each}
            <form onsubmit={(e) => { e.preventDefault(); if (newTag.trim()) { saveTags(detail!, [...(detail!.tags ?? []), newTag.trim()]); newTag = ''; } }}>
              <input class="tagin" bind:value={newTag} placeholder="Add tag…" />
            </form>
          </div>
        </section>
        {#if detail.simple === null && detail.workflow}
          <Badge tone="accent">Made with an advanced workflow ({detail.workflow.nodes.length} nodes)</Badge>
        {/if}
        {#if detail.path_on_server}<p class="faint path selectable">Server file: {detail.path_on_server}</p>{/if}
      </div>
    </aside>
  {/if}
</div>

<Menu bind:open={menu.open} bind:x={menu.x} bind:y={menu.y} bind:items={menu.items} />

<style>
  .srv { padding: 0 12px 8px; }
  .lib { display: flex; height: 100%; min-height: 0; }
  .side { flex-shrink: 0; overflow-y: auto; background: var(--bg-1); padding: 0 10px 20px; }
  .sh { height: 56px; display: flex; align-items: center; padding: 0 8px; }
  .sh h1 { font-size: var(--fs-xl); }
  .group { display: flex; flex-direction: column; gap: 1px; }
  .group button { display: flex; align-items: center; gap: 9px; width: 100%; height: 32px; padding: 0 9px; border: 0; border-radius: var(--r-sm); background: none; color: var(--text-1); cursor: pointer; text-align: left; font-size: var(--fs-md); transition: background var(--t-1); }
  .group button:hover { background: rgba(var(--fg-rgb), 0.045); color: var(--text-0); }
  .group button.on { background: var(--select-bg); color: var(--text-0); }
  .group button :global(svg) { width: 15px; height: 15px; color: var(--text-3); flex-shrink: 0; }
  .group button.on :global(svg) { color: var(--lavender); }
  .group .sub { height: 28px; font-size: var(--fs-sm); color: var(--text-2); }
  .n { margin-left: auto; font-size: var(--fs-xs); color: var(--text-3); }
  .label { display: flex; align-items: center; justify-content: space-between; padding: 16px 9px 4px; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-3); }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; padding: 4px 6px; }
  .tag { display: inline-flex; gap: 5px; align-items: center; padding: 3px 8px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: none; color: var(--text-2); font-size: var(--fs-xs); cursor: pointer; }
  .tag span { color: var(--text-3); }
  .tag.on { background: var(--select-bg); border-color: var(--select-line); color: var(--lavender); }
  .main { flex: 1; min-width: 0; display: flex; flex-direction: column; position: relative; }
  header { display: flex; align-items: center; gap: 8px; height: 56px; padding: 0 16px; border-bottom: 1px solid var(--line-0); }
  .searchbox { width: 300px; }
  .fsel { width: 200px; }
  .fsel.narrow { width: 140px; }
  .spacer { flex: 1; }
  .zoom { width: 110px; accent-color: var(--accent); }
  .bulk { position: absolute; top: 66px; left: 50%; transform: translateX(-50%); z-index: 10; display: flex; align-items: center; gap: 4px; padding: 6px 6px 6px 14px; border-radius: var(--r-lg); background: var(--glass-strong); box-shadow: var(--shadow-3); animation: rise-in var(--t-2) var(--ease-out); min-width: 520px; }
  .grid-wrap { flex: 1; overflow-y: auto; padding: 16px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(var(--th), 1fr)); gap: 10px; }
  .card { position: relative; aspect-ratio: 1; padding: 0; border: 2px solid transparent; border-radius: var(--r-lg); overflow: hidden; background: var(--bg-2); cursor: pointer; transition: transform var(--t-2) var(--ease), border-color var(--t-1), box-shadow var(--t-2); }
  .card:hover { transform: translateY(-2px); box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45); }
  .card.sel { border-color: var(--accent-hi); box-shadow: var(--glow); }
  .card img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cap { position: absolute; left: 0; right: 0; bottom: 0; padding: 24px 10px 8px; font-size: 11.5px; color: var(--text-0); text-align: left; background: linear-gradient(transparent, rgba(0, 0, 0, 0.78)); opacity: 0; transition: opacity var(--t-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card:hover .cap { opacity: 1; }
  .vb { position: absolute; right: 8px; top: 8px; display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: var(--r-full); background: rgba(0, 0, 0, 0.62); color: white; font-size: 11px; }
  .vb :global(svg) { width: 10px; height: 10px; fill: currentColor; }
  .fav { position: absolute; left: 8px; top: 8px; color: var(--warn); }
  .fav :global(svg) { width: 14px; height: 14px; fill: currentColor; }
  .more { display: grid; place-items: center; padding: 20px; }
  .detail { flex-shrink: 0; display: flex; flex-direction: column; background: var(--bg-1); min-height: 0; animation: fade-in var(--t-2); }
  .dh { display: flex; align-items: center; justify-content: space-between; height: 44px; padding: 0 10px 0 16px; font-size: var(--fs-sm); }
  .dprev { padding: 0 14px; }
  .dprev img, .dprev video { width: 100%; max-height: 46vh; object-fit: contain; border-radius: var(--r-md); background: #000; display: block; }
  .dacts { display: flex; align-items: center; gap: 2px; padding: 8px 10px; border-bottom: 1px solid var(--line-0); }
  .dbody { flex: 1; overflow-y: auto; padding: 14px 16px 24px; display: flex; flex-direction: column; gap: 18px; }
  .dbody section { display: flex; flex-direction: column; gap: 6px; }
  .prompt { line-height: 1.6; color: var(--text-0); }
  .neg { color: var(--text-2); font-size: var(--fs-sm); }
  dl { display: grid; grid-template-columns: 110px 1fr; gap: 5px 10px; margin: 0; font-size: var(--fs-sm); }
  dt { color: var(--text-3); }
  dd { margin: 0; color: var(--text-1); overflow-wrap: anywhere; }
  .dtags { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
  .dtag { display: inline-flex; align-items: center; gap: 3px; padding: 2px 4px 2px 9px; border-radius: var(--r-full); background: var(--accent-ghost); color: var(--lavender); font-size: var(--fs-xs); }
  .dtag button { border: 0; background: none; color: inherit; cursor: pointer; display: grid; padding: 1px; }
  .dtag :global(svg) { width: 11px; height: 11px; }
  .tagin { width: 110px; height: 24px; padding: 0 8px; border-radius: var(--r-full); border: 1px dashed var(--line-2); background: none; color: var(--text-0); font-size: var(--fs-xs); outline: none; }
  .path { font-size: var(--fs-xs); overflow-wrap: anywhere; }
</style>
