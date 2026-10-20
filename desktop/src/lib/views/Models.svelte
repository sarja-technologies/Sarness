<script lang="ts">
  import { Boxes, CircleAlert, CircleCheck, Cpu, Download, ExternalLink, HardDrive, Pause, Play, Power, RefreshCw, Search, Trash2, X, Zap } from '@lucide/svelte';
  import { openUrl } from '@tauri-apps/plugin-opener';
  import { apiFor, qs } from '../api/client';
  import type { CatalogEntry, Compatibility, Download as DL, HardwarePreset, HfInspect, HfSearchResult, InstalledModel } from '../api/types';
  import { conn, engineList, onEvent, profileName } from '../stores/connection.svelte';
  import { dropDownload, installCatalog, loadCatalog, loadModels, models, upsertDownload } from '../stores/models.svelte';
  import { toast, toastError } from '../stores/toast.svelte';
  import { isTauri } from '../api/transport';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Empty from '../ui/Empty.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Progress from '../ui/Progress.svelte';
  import Segmented from '../ui/Segmented.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import { confirmDialog } from '../ui/dialogs.svelte';
  import { bytes, mib, rate } from '../util/format';

  let { active }: { active: boolean } = $props();

  // Models are managed per server; with several connected, the header chooses which.
  let server = $state<string | null>(null);
  const srv = $derived(server && conn.engines[server] ? server : conn.activeId);
  const api = $derived(apiFor(srv));
  const mine = $derived(models.by[srv ?? ''] ?? { installed: [], downloads: [], chatModels: [], llm: null, loaded: false });
  const sys = $derived(conn.engines[srv ?? '']?.system ?? null);
  const servers = $derived(engineList().map((e) => ({ value: e.id as string | null, label: profileName(e.id), hint: e.caps?.gpu?.name ?? (e.state === 'online' ? '' : 'offline') })));
  const mineEvent = (e: { engine?: string }) => (e.engine ?? conn.activeId) === srv;
  type Tab = 'installed' | 'catalog' | 'hf' | 'downloads' | 'runtimes';
  let tab = $state<Tab>('catalog');
  let presets = $state<HardwarePreset[]>([]);
  let disk = $state<{ by_category: Record<string, number>; total_bytes: number; free_bytes: number; models_dir: string } | null>(null);
  let runtimes = $state<any>(null);
  let llamaProgress = $state<{ done: number; total: number } | null>(null);
  let loadedOnce = false;
  let shownFor: string | null = null;
  let catFilter = $state<string>('all');

  // Hugging Face
  let hfQuery = $state('');
  let hfKind = $state<'llm' | 'image' | 'video' | 'lora' | ''>('llm');
  let hfResults = $state<HfSearchResult[]>([]);
  let hfSearching = $state(false);
  let inspect = $state<HfInspect | null>(null);
  let inspecting = $state<string | null>(null);
  let hfCategory = $state<string>('llm');

  $effect(() => {
    if (active && !loadedOnce && conn.state === 'online') {
      loadedOnce = true;
      shownFor = srv;
      if (mine.installed.length) tab = 'installed';
      refreshAll();
    }
  });
  $effect(() => {
    const id = srv;
    if (!loadedOnce || id === shownFor) return;
    shownFor = id;
    inspect = null;
    refreshAll();
  });

  async function refreshAll() {
    try {
      await Promise.all([loadCatalog(srv), loadModels(srv)]);
      [presets, disk, runtimes] = await Promise.all([api.get('/v1/models/presets'), api.get('/v1/models/disk'), api.get('/v1/runtimes')]);
    } catch (e) {
      toastError(e, 'Could not load models');
    }
  }

  onEvent('runtime.progress', (e) => mineEvent(e) && (llamaProgress = { done: e.data.done, total: e.data.total }));
  onEvent('runtime.installed', (e) => { if (!mineEvent(e)) return; llamaProgress = null; toast('success', 'llama.cpp installed', e.data.version); refreshAll(); });
  onEvent('runtime.failed', (e) => { if (!mineEvent(e)) return; llamaProgress = null; toast('error', 'llama.cpp install failed', e.data.error); refreshAll(); });
  onEvent('models.changed', (e) => { if (loadedOnce && mineEvent(e)) api.get('/v1/models/disk').then((d) => (disk = d)).catch(() => {}); });

  const gpu = $derived(sys?.hardware.gpus[0]);
  const activeDownloads = $derived(mine.downloads.filter((d) => ['queued', 'downloading', 'paused'].includes(d.status)));
  const catalog = $derived(models.catalog.filter((c) => catFilter === 'all' || c.category === catFilter));
  const match = $derived(presets.find((p) => p.matches_this_server));

  function compatTone(c: Compatibility) {
    return ({ fits: 'ok', offload: 'warn', too_large: 'err', unknown: 'neutral' } as const)[c.level];
  }

  async function action(d: DL, a: 'pause' | 'resume' | 'cancel' | 'dismiss') {
    try {
      const r = await api.post<DL>(`/v1/downloads/${d.id}/${a}`);
      if (a === 'dismiss') dropDownload(srv!, d.id);
      else upsertDownload({ ...r, engine: srv ?? undefined });
    } catch (e) {
      toastError(e);
    }
  }

  async function removeModel(m: InstalledModel) {
    if (!(await confirmDialog(`Delete ${m.name}?`, `This frees ${bytes(m.size)} on the server. Generations made with it are kept.`, { danger: true, confirmLabel: 'Delete files' }))) return;
    try {
      await api.del(`/v1/models/${encodeURIComponent(m.id)}`);
      toast('success', 'Model removed', m.name);
      loadModels(srv);
    } catch (e) {
      toastError(e, 'Could not delete model');
    }
  }

  async function llm(m: InstalledModel, load: boolean) {
    try {
      if (load) await api.post('/v1/llm/load', { model_id: m.id });
      else await api.post('/v1/llm/unload');
      loadModels(srv);
    } catch (e) {
      toastError(e);
    }
  }

  async function hfSearch() {
    if (!hfQuery.trim()) return;
    hfSearching = true;
    inspect = null;
    try {
      hfResults = await api.get<HfSearchResult[]>(`/v1/hf/search${qs({ q: hfQuery.trim(), kind: hfKind })}`);
    } catch (e) {
      toastError(e, 'Search failed');
    } finally {
      hfSearching = false;
    }
  }

  async function hfInspect(repo: string) {
    inspecting = repo;
    try {
      inspect = await api.get<HfInspect>(`/v1/hf/inspect${qs({ repo })}`);
      hfCategory = inspect.category ?? (hfKind || 'lora');
    } catch (e) {
      toastError(e, 'Could not read repository');
    } finally {
      inspecting = null;
    }
  }

  async function hfInstall(files: string[]) {
    if (!inspect) return;
    try {
      const d = await api.post<DL>('/v1/models/install', { repo: inspect.repo, files, category: hfCategory, name: inspect.repo.split('/')[1] });
      upsertDownload({ ...d, engine: srv ?? undefined });
      toast('info', 'Download started', inspect.repo);
      tab = 'downloads';
    } catch (e) {
      toastError(e, 'Could not start download');
    }
  }

  async function installLlama() {
    try {
      await api.post('/v1/runtimes/llama/install', {});
      llamaProgress = { done: 0, total: 0 };
    } catch (e) {
      toastError(e);
    }
  }

  const catLabels: Record<string, string> = { llm: 'Language', image: 'Image', video: 'Video', upscaler: 'Upscaler', vae: 'VAE', lora: 'LoRA', controlnet: 'ControlNet' };
</script>

<div class="models">
  <header>
    <h1>Models</h1>
    {#if servers.length > 1}<Select value={srv} options={servers} size="sm" onchange={(v) => (server = v)} />{/if}
    <Segmented bind:value={tab} items={[
      { value: 'installed', label: `Installed${mine.installed.length ? ` · ${mine.installed.length}` : ''}` },
      { value: 'catalog', label: 'Catalog' },
      { value: 'hf', label: 'Hugging Face' },
      { value: 'downloads', label: `Downloads${activeDownloads.length ? ` · ${activeDownloads.length}` : ''}` },
      { value: 'runtimes', label: 'Runtimes' },
    ]} />
    <div class="spacer"></div>
    {#if disk}<span class="disk"><HardDrive />{bytes(disk.total_bytes)} used · {bytes(disk.free_bytes)} free</span>{/if}
    <IconButton label="Rescan model folders" onclick={async () => { await api.post('/v1/models/rescan'); refreshAll(); }}><RefreshCw /></IconButton>
  </header>

  <div class="body">
    {#if tab === 'installed'}
      {#if !mine.installed.length}
        <Empty icon={Boxes} title="No models installed" body="Start with the catalog: it lists models that are known to work well on your hardware.">
          <Button variant="primary" onclick={() => (tab = 'catalog')}>Open catalog</Button>
        </Empty>
      {:else}
        {#each Object.keys(catLabels).filter((c) => mine.installed.some((m) => m.category === c)) as cat (cat)}
          <h2 class="sect">{catLabels[cat]}</h2>
          <div class="list">
            {#each mine.installed.filter((m) => m.category === cat) as m (m.id)}
              <div class="row card">
                <div class="info">
                  <div class="name">{m.name}
                    {#if m.loaded}<Badge tone="ok" dot>Loaded</Badge>{/if}
                    {#if !m.supported}<Badge tone="err">Unsupported</Badge>{/if}
                    {#if m.capabilities?.vision}<Badge tone="info">Vision</Badge>{/if}
                    {#if m.remote}<Badge tone="info">Server gateway</Badge>{/if}
                  </div>
                  <div class="sub">{#if m.remote}Runs on the server's AI gateway{#if m.description} · {m.description}{/if}{:else}{m.family ?? '—'} · {m.format} · {bytes(m.size)}{#if m.vram_mb} · ~{mib(m.vram_mb)} VRAM{/if}{#if m.quantization} · {m.quantization}{/if}{/if}</div>
                  {#if m.unsupported_reason}<div class="warn"><CircleAlert />{m.unsupported_reason}</div>{/if}
                  <div class="path mono selectable">{m.path}</div>
                </div>
                <div class="acts">
                  {#if m.category === 'llm'}
                    {#if m.loaded}<Button size="sm" variant="ghost" onclick={() => llm(m, false)}><Power />Unload</Button>
                    {:else}<Button size="sm" variant="ghost" onclick={() => llm(m, true)}><Zap />Load</Button>{/if}
                  {/if}
                  {#if !m.remote}<IconButton label="Delete model files" tone="danger" onclick={() => removeModel(m)}><Trash2 /></IconButton>{/if}
                </div>
              </div>
            {/each}
          </div>
        {/each}
      {/if}

    {:else if tab === 'catalog'}
      {#if gpu}
        <div class="hw card">
          <Cpu />
          <div>
            <strong>{#if gpu.unified_memory}{gpu.name} · {mib(gpu.vram_total_mb)} unified memory{:else}{gpu.name} · {mib(gpu.vram_total_mb)} VRAM · {mib(sys?.hardware.memory.total_mb)} RAM{/if}</strong>
            {#if match}<p class="muted">{match.notes}</p>{/if}
          </div>
        </div>
      {/if}
      <div class="filters">
        <Segmented bind:value={catFilter} size="sm" items={[{ value: 'all', label: 'All' }, ...Object.entries(catLabels).map(([value, label]) => ({ value, label }))]} />
      </div>
      <div class="cat-grid">
        {#each catalog as c (c.id)}
          {@const rec = match?.recommended.includes(c.id)}
          <article class="entry card" class:rec>
            <div class="eh">
              <span class="ecat">{catLabels[c.category]}</span>
              {#if rec}<Badge tone="accent">Recommended for this server</Badge>{/if}
              {#if c.installed}<Badge tone="ok" dot>Installed</Badge>{/if}
            </div>
            <h3>{c.name}</h3>
            <p class="desc">{c.description}</p>
            {#if c.notes}<p class="note">{c.notes}</p>{/if}
            <div class="meta">
              <Badge tone={compatTone(c.compatibility)}>{c.compatibility.label}</Badge>
              {#if c.license}<span class="faint">{c.license}</span>{/if}
            </div>
            {#if c.quantizations}
              <div class="quants">
                {#each c.quantizations as q (q.id)}
                  <div class="quant">
                    <span class="qid mono">{q.id}</span>
                    <span class="faint">{q.size_gb} GB · ~{mib(q.vram_mb)} VRAM</span>
                    <span class="spacer"></span>
                    {#if q.installed}<CircleCheck class="okic" />
                    {:else if q.download && q.download.status !== 'failed' && q.download.status !== 'cancelled'}<span class="faint">{Math.round((q.download.done_bytes / (q.download.total_bytes || 1)) * 100)}%</span>
                    {:else}<Button size="sm" variant="subtle" onclick={() => installCatalog(c.id, q.id, srv).then(() => loadCatalog(srv))}><Download />Install</Button>{/if}
                  </div>
                {/each}
              </div>
            {:else}
              <div class="foot">
                <span class="faint">{c.size_gb} GB download{c.vram_mb ? ` · ~${mib(c.vram_mb)} VRAM` : ''}{c.vram_offload_mb ? ` (${mib(c.vram_offload_mb)} with offload)` : ''}</span>
                <span class="spacer"></span>
                {#if c.installed}<CircleCheck class="okic" />
                {:else if c.download && c.download.status !== 'failed' && c.download.status !== 'cancelled'}<span class="faint">{Math.round((c.download.done_bytes / (c.download.total_bytes || 1)) * 100)}%</span>
                {:else}<Button size="sm" variant="subtle" onclick={() => installCatalog(c.id, undefined, srv).then(() => loadCatalog(srv))}><Download />Install</Button>{/if}
              </div>
            {/if}
            <button class="repo" onclick={() => isTauri ? openUrl(`https://huggingface.co/${c.repo}`) : window.open(`https://huggingface.co/${c.repo}`)}>{c.repo}<ExternalLink /></button>
          </article>
        {/each}
      </div>

    {:else if tab === 'hf'}
      <form class="hfbar" onsubmit={(e) => { e.preventDefault(); hfSearch(); }}>
        <div class="hfq"><TextInput bind:value={hfQuery} placeholder="Search Hugging Face (e.g. Qwen3 GGUF, sdxl lora)">{#snippet icon()}<Search />{/snippet}</TextInput></div>
        <Segmented bind:value={hfKind} items={[{ value: 'llm', label: 'GGUF LLMs' }, { value: 'image', label: 'Diffusers' }, { value: 'lora', label: 'LoRAs' }, { value: '', label: 'Any' }]} />
        <Button variant="primary" type="submit" loading={hfSearching}>Search</Button>
      </form>
      <p class="faint small">Only GGUF language models, SDXL / SD 1.5 / Wan Diffusers pipelines, LoRAs, VAEs, ControlNets and ESRGAN-style upscalers can run in Sarness. Unsupported repositories are flagged before download.</p>
      <div class="hfsplit">
        <div class="hflist">
          {#each hfResults as r (r.id)}
            <button class="hfitem" class:on={inspect?.repo === r.id} onclick={() => hfInspect(r.id)}>
              <span class="truncate hfname">{r.id}</span>
              <span class="faint small">{r.library ?? r.pipeline_tag ?? '—'} · {Intl.NumberFormat(undefined, { notation: 'compact' }).format(r.downloads ?? 0)} downloads{r.gated ? ' · gated' : ''}</span>
              {#if inspecting === r.id}<Spinner size={12} />{/if}
            </button>
          {/each}
          {#if !hfResults.length && !hfSearching}<p class="faint small pad">Search to browse models.</p>{/if}
        </div>
        <div class="hfdetail">
          {#if inspect}
            <h3>{inspect.repo}</h3>
            <div class="meta">
              <Badge>{inspect.format}</Badge>
              {#if inspect.pipeline}<Badge>{inspect.pipeline}</Badge>{/if}
              {#if inspect.supported === false}<Badge tone="err">Not supported</Badge>{/if}
              <span class="faint">{bytes(inspect.total_bytes)} in repo</span>
            </div>
            {#if inspect.unsupported_reason}<p class="warn"><CircleAlert />{inspect.unsupported_reason}</p>{/if}
            <div class="row" style="margin:10px 0">
              <span class="small muted">Install as</span>
              <div style="width:180px"><Select bind:value={hfCategory} size="sm" options={Object.entries(catLabels).map(([value, label]) => ({ value, label }))} /></div>
            </div>
            <div class="opts">
              {#each inspect.options as o (o.label)}
                <div class="opt card">
                  <div class="info">
                    <div class="name mono">{o.label}{#if o.vision}<Badge tone="info">+ vision projector</Badge>{/if}</div>
                    <div class="sub">{bytes(o.size_bytes)} · ~{mib(o.vram_mb)} VRAM · {o.files.length} file{o.files.length > 1 ? 's' : ''}</div>
                  </div>
                  <Badge tone={compatTone(o.compatibility)}>{o.compatibility.label}</Badge>
                  <Button size="sm" variant="subtle" disabled={inspect.supported === false} onclick={() => hfInstall(o.files)}><Download />Install</Button>
                </div>
              {/each}
              {#if !inspect.options.length}<p class="faint">No installable files found.</p>{/if}
            </div>
          {:else}
            <Empty icon={Search} title="Pick a repository" body="Sarness checks its format, size and whether it fits your GPU before downloading anything." compact />
          {/if}
        </div>
      </div>

    {:else if tab === 'downloads'}
      {#if !mine.downloads.length}
        <Empty icon={Download} title="No downloads" body="Downloads resume automatically after interruptions or engine restarts." />
      {/if}
      <div class="list">
        {#each mine.downloads as d (d.id)}
          <div class="row card dl">
            <div class="info">
              <div class="name">{d.name}
                <Badge tone={d.status === 'completed' ? 'ok' : d.status === 'failed' ? 'err' : d.status === 'paused' ? 'warn' : d.status === 'downloading' ? 'accent' : 'neutral'}>{d.status}</Badge>
              </div>
              <div class="sub tnum">{bytes(d.done_bytes)} of {bytes(d.total_bytes)}{d.status === 'downloading' && d.speed_bps ? ` · ${rate(d.speed_bps)}` : ''}{d.status === 'downloading' && d.speed_bps ? ` · ${Math.ceil((d.total_bytes - d.done_bytes) / d.speed_bps / 60)} min left` : ''}</div>
              {#if d.status !== 'completed'}<Progress value={d.total_bytes ? d.done_bytes / d.total_bytes : 0} tone={d.status === 'failed' ? 'err' : d.status === 'paused' ? 'warn' : 'accent'} />{/if}
              {#if d.error}<div class="warn selectable"><CircleAlert />{d.error}</div>{/if}
            </div>
            <div class="acts">
              {#if d.status === 'downloading' || d.status === 'queued'}<IconButton label="Pause" onclick={() => action(d, 'pause')}><Pause /></IconButton>{/if}
              {#if d.status === 'paused' || d.status === 'failed'}<IconButton label="Resume" onclick={() => action(d, 'resume')}><Play /></IconButton>{/if}
              {#if d.status !== 'completed' && d.status !== 'cancelled'}<IconButton label="Cancel and delete partial files" tone="danger" onclick={() => action(d, 'cancel')}><X /></IconButton>{/if}
              {#if d.status === 'completed' || d.status === 'cancelled' || d.status === 'failed'}<IconButton label="Dismiss" onclick={() => action(d, 'dismiss')}><Trash2 /></IconButton>{/if}
            </div>
          </div>
        {/each}
      </div>

    {:else if tab === 'runtimes' && runtimes}
      <div class="list">
        <div class="row card">
          <div class="info">
            <div class="name">llama.cpp server {#if runtimes.llama_cpp.installed}<Badge tone="ok" dot>{runtimes.llama_cpp.version}</Badge>{:else}<Badge tone="warn">Not installed</Badge>{/if}</div>
            <div class="sub">Runs GGUF chat models. Official prebuilt binaries ({runtimes.llama_cpp.suggested_variant} suggested for this server).</div>
            {#if llamaProgress}<Progress value={llamaProgress.total ? llamaProgress.done / llamaProgress.total : null} />{/if}
            {#if runtimes.llama_cpp.install?.error}<div class="warn"><CircleAlert />{runtimes.llama_cpp.install.error}</div>{/if}
            {#if runtimes.llama_cpp.path}<div class="path mono">{runtimes.llama_cpp.path}</div>{/if}
          </div>
          <Button variant={runtimes.llama_cpp.installed ? 'ghost' : 'primary'} loading={!!llamaProgress} onclick={installLlama}>{runtimes.llama_cpp.installed ? 'Update' : 'Install'}</Button>
        </div>
        {#each [['PyTorch', runtimes.torch], ['Diffusers', runtimes.diffusers], ['Transformers', runtimes.transformers], ['Spandrel (upscalers)', runtimes.spandrel], ['FFmpeg (imageio)', runtimes.imageio_ffmpeg]] as [n, v] (n)}
          <div class="row card"><div class="info"><div class="name">{n} {#if v}<Badge tone="ok" dot>{v}</Badge>{:else}<Badge tone="err">Missing</Badge>{/if}</div></div></div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .models { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  header { display: flex; align-items: center; gap: 16px; height: 56px; padding: 0 16px 0 22px; border-bottom: 1px solid var(--line-0); flex-shrink: 0; }
  h1 { font-size: var(--fs-xl); }
  .spacer { flex: 1; }
  .disk { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-xs); color: var(--text-2); }
  .disk :global(svg) { width: 13px; height: 13px; }
  .body { flex: 1; overflow-y: auto; padding: 20px 24px 40px; }
  .sect { font-size: var(--fs-sm); color: var(--text-2); text-transform: uppercase; letter-spacing: 0.08em; margin: 18px 0 8px; }
  .sect:first-child { margin-top: 0; }
  .card { border-radius: var(--r-lg); background: var(--bg-2); border: 1px solid var(--line-1); }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .row { display: flex; align-items: center; gap: 16px; padding: 14px 16px; }
  .info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
  .name { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--text-0); flex-wrap: wrap; }
  .sub { font-size: var(--fs-sm); color: var(--text-2); }
  .path { font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .warn { display: flex; align-items: flex-start; gap: 6px; color: var(--warn); font-size: var(--fs-sm); }
  .warn :global(svg) { width: 14px; height: 14px; flex-shrink: 0; margin-top: 2px; }
  .acts { display: flex; align-items: center; gap: 4px; }
  .hw { display: flex; align-items: center; gap: 14px; padding: 14px 16px; margin-bottom: 16px; background: radial-gradient(100% 160% at 0% 0%, rgba(var(--accent-rgb), 0.14), transparent 60%), var(--bg-2); }
  .hw > :global(svg) { width: 22px; height: 22px; color: var(--lavender); }
  .hw p { font-size: var(--fs-sm); margin-top: 2px; }
  .filters { margin-bottom: 14px; }
  .cat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 12px; }
  .entry { display: flex; flex-direction: column; gap: 9px; padding: 16px; transition: border-color var(--t-1), transform var(--t-2) var(--ease); }
  .entry:hover { border-color: var(--line-2); }
  .entry.rec { border-color: rgba(var(--accent-rgb), 0.3); }
  .eh { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .ecat { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-3); font-weight: 700; margin-right: auto; }
  .desc { font-size: var(--fs-sm); color: var(--text-1); line-height: 1.55; }
  .note { font-size: var(--fs-xs); color: var(--text-2); line-height: 1.5; padding: 6px 9px; border-radius: var(--r-sm); background: var(--info-bg); }
  .meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-xs); }
  .quants { display: flex; flex-direction: column; gap: 4px; margin-top: 2px; }
  .quant, .foot { display: flex; align-items: center; gap: 10px; font-size: var(--fs-sm); padding: 6px 8px; border-radius: var(--r-md); background: var(--bg-1); }
  .qid { color: var(--text-0); font-size: var(--fs-sm); }
  :global(.okic) { width: 17px; height: 17px; color: var(--ok); }
  .repo { display: inline-flex; align-items: center; gap: 5px; align-self: flex-start; border: 0; background: none; color: var(--text-3); font-size: var(--fs-xs); cursor: pointer; padding: 0; font-family: var(--font-mono); }
  .repo:hover { color: var(--lavender); }
  .repo :global(svg) { width: 11px; height: 11px; }
  .hfbar { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
  .hfq { flex: 1; max-width: 520px; }
  .small { font-size: var(--fs-xs); }
  .pad { padding: 12px; }
  .hfsplit { display: grid; grid-template-columns: minmax(280px, 380px) 1fr; gap: 16px; margin-top: 14px; min-height: 400px; }
  .hflist { display: flex; flex-direction: column; gap: 3px; max-height: calc(100vh - 260px); overflow-y: auto; }
  .hfitem { display: flex; flex-direction: column; gap: 2px; padding: 9px 11px; border-radius: var(--r-md); border: 1px solid transparent; background: none; text-align: left; color: var(--text-1); cursor: pointer; }
  .hfitem:hover { background: rgba(var(--fg-rgb), 0.04); }
  .hfitem.on { background: var(--select-bg); border-color: var(--select-line); }
  .hfname { font-weight: 600; color: var(--text-0); font-size: var(--fs-sm); }
  .hfdetail h3 { margin-bottom: 8px; font-family: var(--font-mono); font-size: var(--fs-md); }
  .opts { display: flex; flex-direction: column; gap: 6px; }
  .opt { display: flex; align-items: center; gap: 12px; padding: 10px 12px; }
  .dl .info { gap: 7px; }
</style>
