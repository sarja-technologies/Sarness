<script lang="ts">
  import { Palette } from '@lucide/svelte';
  import Segmented from '../ui/Segmented.svelte';
  import { prefs, persistPrefs } from '../stores/prefs.svelte';
  import { applyTheme } from '../stores/theme';
  import { Cable, FolderPlus, HardDrive, Info, KeyRound, Pencil, Plug, Plus, RefreshCw, Server, Sliders, Trash2, Unplug } from '@lucide/svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { api } from '../api/client';
  import { isTauri } from '../api/transport';
  import type { EngineSettings } from '../api/types';
  import ServerForm from '../components/ServerForm.svelte';
  import { conn, connect, deleteProfile, disconnect, type Profile } from '../stores/connection.svelte';
  import { loadModels } from '../stores/models.svelte';
  import { toast, toastError } from '../stores/toast.svelte';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Field from '../ui/Field.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Modal from '../ui/Modal.svelte';
  import Slider from '../ui/Slider.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { confirmDialog } from '../ui/dialogs.svelte';
  import { bytes } from '../util/format';

  type Section = 'appearance' | 'servers' | 'engine' | 'endpoints' | 'desktop' | 'about';
  let section = $state<Section>('appearance');
  let editing = $state<Profile | null | 'new'>(null);
  let s = $state<EngineSettings | null>(null);
  let hfToken = $state('');
  let gwUrl = $state('');
  let gwToken = $state('');
  let gw = $state<{ configured: boolean; url: string | null; error: string | null; models: string[] } | null>(null);
  let gwBusy = $state(false);
  let saving = $state(false);
  let cache = $state<{ path: string; bytes: number; limit_bytes: number } | null>(null);
  let app = $state<any>(null);
  let newDir = $state('');
  let ep = $state({ name: '', base_url: '', api_key: '', vision: false });

  async function loadEngine() {
    try {
      s = await api.get<EngineSettings>('/v1/settings');
      gwUrl = s.gateway_url ?? '';
      gw = await api.get('/v1/gateway');
    } catch (e) {
      toastError(e, 'Could not load engine settings');
    }
  }
  async function loadDesktop() {
    if (!isTauri) return;
    [cache, app] = await Promise.all([invoke<any>('cache_info'), invoke<any>('app_info')]);
  }
  $effect(() => {
    if ((section === 'engine' || section === 'endpoints') && !s && conn.state === 'online') loadEngine();
    if ((section === 'desktop' || section === 'about') && !cache) loadDesktop();
  });

  async function saveGateway() {
    gwBusy = true;
    try {
      const body: Record<string, unknown> = { gateway_url: gwUrl.trim() };
      if (gwToken) body.gateway_token = gwToken;
      s = await api.patch<EngineSettings>('/v1/settings', body);
      gwToken = '';
      gw = await api.get('/v1/gateway');
      await loadModels();
      if (gw?.error) toast('error', `Gateway: ${gw.error}`);
      else toast('success', gw?.configured ? `Gateway connected · ${gw.models.length} models` : 'Gateway removed');
    } catch (e) {
      toastError(e, 'Could not save the gateway');
    } finally {
      gwBusy = false;
    }
  }

  async function patch(p: Partial<EngineSettings> & Record<string, unknown>, msg = 'Settings saved') {
    saving = true;
    try {
      s = await api.patch<EngineSettings>('/v1/settings', p);
      toast('success', msg);
    } catch (e) {
      toastError(e, 'Could not save');
    } finally {
      saving = false;
    }
  }

  async function removeProfile(p: Profile) {
    if (!(await confirmDialog(`Remove ${p.name}?`, 'The connection details and stored token are removed from this desktop. Nothing changes on the server.', { danger: true, confirmLabel: 'Remove' }))) return;
    await deleteProfile(p.id);
  }

  async function addEndpoint() {
    if (!s || !ep.base_url.trim()) return;
    const id = Math.random().toString(36).slice(2, 10);
    await patch({ llm_endpoints: [...s.llm_endpoints, { id, name: ep.name || ep.base_url, base_url: ep.base_url.trim().replace(/\/$/, ''), api_key: ep.api_key || false, vision: ep.vision }] as any }, 'Endpoint added');
    try {
      await api.post(`/v1/llm/endpoints/${id}/refresh`);
      await loadEngine();
      loadModels();
    } catch (e) {
      toastError(e, 'Saved, but listing its models failed');
    }
    ep = { name: '', base_url: '', api_key: '', vision: false };
  }

  function setTheme(t: 'light' | 'dark' | 'system') {
    prefs.theme = t;
    persistPrefs();
    applyTheme(t);
  }

  const nav: { id: Section; label: string; icon: any }[] = [
    { id: 'appearance', label: 'Appearance', icon: Palette },
    { id: 'servers', label: 'Servers', icon: Server },
    { id: 'engine', label: 'Engine & resources', icon: Sliders },
    { id: 'endpoints', label: 'Remote chat endpoints', icon: Cable },
    { id: 'desktop', label: 'Desktop & storage', icon: HardDrive },
    { id: 'about', label: 'About', icon: Info },
  ];
</script>

<div class="settings">
  <nav>
    <h1>Settings</h1>
    {#each nav as n (n.id)}
      <button class:on={section === n.id} onclick={() => (section = n.id)}><n.icon />{n.label}</button>
    {/each}
  </nav>

  <div class="content">
    {#if section === 'appearance'}
      <div class="head"><h2>Appearance</h2></div>
      <div class="group">
        <h3>Theme</h3>
        <p class="muted small">Light is the default. System follows your operating system.</p>
        <Segmented value={prefs.theme} onchange={setTheme} items={[{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }, { value: 'system', label: 'System' }]} />
      </div>
    {:else if section === 'servers'}
      <div class="head"><h2>Servers</h2><Button variant="primary" size="sm" onclick={() => (editing = 'new')}><Plus />Add server</Button></div>
      <p class="muted lead">Sarness connects to a Sarness Engine over your LAN, Tailscale or an SSH tunnel. Tokens live in your system keyring.</p>
      <div class="list">
        {#each conn.profiles as p (p.id)}
          {@const isActive = conn.activeId === p.id}
          <div class="card prof" class:active={isActive}>
            <span class="led" class:on={isActive && conn.state === 'online'} class:off={isActive && conn.state === 'offline'}></span>
            <div class="info">
              <div class="name">{p.name} {#if isActive}<Badge tone={conn.state === 'online' ? 'ok' : 'warn'}>{conn.state}</Badge>{/if}</div>
              <div class="sub mono">{p.mode === 'ssh' ? `ssh ${p.ssh?.user}@${p.ssh?.host} → ${p.host}:${p.port}` : `${p.scheme}://${p.host}:${p.port}`}</div>
            </div>
            {#if !p.has_token}<Badge tone="warn"><KeyRound />No token</Badge>{/if}
            {#if isActive && conn.state !== 'offline'}
              <Button size="sm" variant="ghost" onclick={() => disconnect()}><Unplug />Disconnect</Button>
            {:else}
              <Button size="sm" variant="subtle" onclick={() => connect(p.id)}><Plug />Connect</Button>
            {/if}
            <IconButton label="Edit" onclick={() => (editing = p)}><Pencil /></IconButton>
            <IconButton label="Remove" tone="danger" onclick={() => removeProfile(p)}><Trash2 /></IconButton>
          </div>
        {/each}
      </div>
      {#if conn.lastError}<p class="err selectable">Last error: {conn.lastError}</p>{/if}

    {:else if section === 'engine'}
      <div class="head"><h2>Engine & resources</h2><IconButton label="Reload" onclick={loadEngine}><RefreshCw /></IconButton></div>
      {#if s}
        <div class="group">
          <h3>GPU scheduling</h3>
          <Toggle bind:checked={s.auto_unload_llm_for_generation} label="Unload the chat model when a generation needs its VRAM" hint="Only idle models are unloaded; jobs wait while a reply is streaming." onchange={(v) => patch({ auto_unload_llm_for_generation: v })} />
          <Toggle bind:checked={s.oom_retry_with_offload} label="Retry once with CPU offload after running out of GPU memory" onchange={(v) => patch({ oom_retry_with_offload: v })} />
          <Slider bind:value={s.vram_headroom_mb} min={0} max={3000} step={100} label="VRAM headroom" unit=" MB" onchange={(v) => patch({ vram_headroom_mb: v })} />
          <Slider bind:value={s.worker_idle_unload_minutes} min={0} max={120} label="Release diffusion models after idle" format={(v) => (v ? `${v} min` : 'Never')} onchange={(v) => patch({ worker_idle_unload_minutes: v })} />
          <Slider bind:value={s.llm_idle_unload_minutes} min={0} max={240} step={5} label="Unload chat model after idle" format={(v) => (v ? `${v} min` : 'Never')} onchange={(v) => patch({ llm_idle_unload_minutes: v })} />
          <Slider bind:value={s.job_max_attempts} min={1} max={5} label="Max attempts per job (after crashes/restarts)" onchange={(v) => patch({ job_max_attempts: v })} />
        </div>
        <div class="group">
          <h3>Chat model defaults</h3>
          <Slider bind:value={s.llm_default_ctx} min={2048} max={131072} step={1024} label="Default context length" format={(v) => `${Math.round(v / 1024)}K`} onchange={(v) => patch({ llm_default_ctx: v })} />
          <Toggle bind:checked={s.llm_flash_attn} label="Flash attention" hint="Faster and lighter on VRAM on supported GPUs." onchange={(v) => patch({ llm_flash_attn: v })} />
        </div>
        <div class="group">
          <h3>Server AI gateway</h3>
          <p class="muted small">If the server already runs its own AI service that owns the GPU, Sarness can send image and video jobs to it instead of loading a second copy of the models. Its models appear in Images and Videos with a <em>server gateway</em> tag; results still land in your Sarness library.</p>
          <Field label="Gateway URL" hint="As seen from the engine, e.g. http://127.0.0.1:8080 when it runs on the same server.">
            <TextInput bind:value={gwUrl} placeholder="http://127.0.0.1:8080" mono size="sm" />
          </Field>
          <Field label="API token" hint={s.gateway_token ? 'A token is stored on the server. Enter a new one to replace it.' : 'Stored on the server, never shown again.'}>
            <TextInput bind:value={gwToken} type="password" placeholder={s.gateway_token ? '•••••••• (stored)' : 'Bearer token'} mono size="sm" />
          </Field>
          <div class="row">
            <Button size="sm" loading={gwBusy} onclick={saveGateway}><Plug />{gwUrl.trim() ? 'Save & connect' : 'Remove gateway'}</Button>
            {#if gw?.configured}<Button size="sm" variant="ghost" onclick={async () => { gw = await api.post('/v1/gateway/refresh'); await loadModels(); }}><RefreshCw />Refresh models</Button>{/if}
          </div>
          {#if gw?.configured}
            <p class="small {gw.error ? 'err' : 'faint'}">{gw.error ? `Not reachable: ${gw.error}` : `Connected · ${gw.models.length} models: ${gw.models.map((m) => m.replace(/^gw:/, '')).join(', ')}`}</p>
          {/if}
        </div>
        <div class="group">
          <h3>Hugging Face</h3>
          <Field label="Access token" hint={s.hf_token ? 'A token is stored on the server. Enter a new one to replace it.' : 'Needed only for gated models. Stored on the server, never shown again.'}>
            <div class="row">
              <TextInput bind:value={hfToken} type="password" placeholder={s.hf_token ? '•••••••• (stored)' : 'hf_…'} mono />
              <Button size="sm" disabled={!hfToken} onclick={() => { patch({ hf_token: hfToken }, 'Token saved'); hfToken = ''; }}>Save</Button>
              {#if s.hf_token}<Button size="sm" variant="ghost" onclick={() => patch({ hf_token: '' }, 'Token removed')}>Remove</Button>{/if}
            </div>
          </Field>
        </div>
        <div class="group">
          <h3>Model locations</h3>
          <p class="muted small">Models are installed into the engine's models folder. Additional folders on the server are scanned (read-only) for existing models using the same layout (<code>llm/</code>, <code>image/</code>, <code>video/</code>, <code>lora/</code>…).</p>
          {#each s.extra_model_dirs as d, i (d)}
            <div class="row dir"><span class="mono">{d}</span><span class="spacer"></span><IconButton label="Remove" size="sm" onclick={() => patch({ extra_model_dirs: s!.extra_model_dirs.filter((_, j) => j !== i) })}><Trash2 /></IconButton></div>
          {/each}
          <form class="row" onsubmit={(e) => { e.preventDefault(); if (newDir.trim()) { patch({ extra_model_dirs: [...s!.extra_model_dirs, newDir.trim()] }); newDir = ''; } }}>
            <TextInput bind:value={newDir} placeholder="/mnt/models" mono size="sm" />
            <Button size="sm" type="submit"><FolderPlus />Add folder</Button>
          </form>
          {#if conn.system}<p class="faint small">Engine data: <span class="mono">{conn.system.engine.data_dir}</span></p>{/if}
        </div>
      {/if}

    {:else if section === 'endpoints'}
      <div class="head"><h2>Remote chat endpoints</h2></div>
      <p class="muted lead">Add OpenAI-compatible servers (another llama.cpp server, vLLM, LM Studio, Ollama's /v1 API…). The engine proxies chat to them; keys are stored on the engine only.</p>
      {#if s}
        <div class="list">
          {#each s.llm_endpoints as e (e.id)}
            <div class="card prof">
              <div class="info">
                <div class="name">{e.name} {#if e.vision}<Badge tone="info">vision</Badge>{/if}</div>
                <div class="sub mono">{e.base_url} · {(e.models ?? []).length} models{e.api_key ? ' · key stored' : ''}</div>
              </div>
              <IconButton label="Refresh model list" onclick={async () => { try { await api.post(`/v1/llm/endpoints/${e.id}/refresh`); await loadEngine(); loadModels(); toast('success', 'Models refreshed'); } catch (err) { toastError(err); } }}><RefreshCw /></IconButton>
              <IconButton label="Remove" tone="danger" onclick={() => patch({ llm_endpoints: s!.llm_endpoints.filter((x) => x.id !== e.id) as any }, 'Endpoint removed').then(() => loadModels())}><Trash2 /></IconButton>
            </div>
          {/each}
        </div>
        <div class="group">
          <h3>Add endpoint</h3>
          <div class="g2">
            <Field label="Name"><TextInput bind:value={ep.name} placeholder="Workstation vLLM" /></Field>
            <Field label="Base URL" hint="Must include /v1, e.g. http://100.64.0.9:8000/v1"><TextInput bind:value={ep.base_url} placeholder="http://host:port/v1" mono /></Field>
          </div>
          <Field label="API key (optional)"><TextInput bind:value={ep.api_key} type="password" mono /></Field>
          <Toggle bind:checked={ep.vision} label="Models accept images" hint="Only enable if the endpoint's models are multimodal." />
          <div><Button variant="primary" onclick={addEndpoint} disabled={!ep.base_url} loading={saving}><Plus />Add endpoint</Button></div>
        </div>
      {/if}

    {:else if section === 'desktop'}
      <div class="head"><h2>Desktop & storage</h2></div>
      {#if cache}
        <div class="group">
          <h3>Media cache</h3>
          <p class="muted small">Generated files are kept canonically on the server. This desktop caches what you view so the library loads instantly and stays viewable offline.</p>
          <div class="row"><span class="mono small">{cache.path}</span></div>
          <div class="row"><Badge>{bytes(cache.bytes)} of {bytes(cache.limit_bytes)} limit</Badge><span class="spacer"></span>
            <Button size="sm" variant="ghost" onclick={async () => { const freed = await invoke<number>('clear_cache'); toast('success', 'Cache cleared', `${bytes(freed)} freed`); loadDesktop(); }}>Clear cache</Button>
          </div>
        </div>
        <div class="group">
          <h3>Saved files</h3>
          <p class="muted small">“Save” and “Open in folder” copy outputs to:</p>
          <div class="mono small">{app?.export_images}</div>
          <div class="mono small">{app?.export_videos}</div>
        </div>
      {:else if !isTauri}
        <p class="muted">Desktop settings are available in the Sarness app.</p>
      {/if}

    {:else if section === 'about'}
      <div class="head"><h2>About Sarness</h2></div>
      <div class="group">
        <p><strong>Sarness</strong> (Sarja Harness) — self-hosted AI workstation.</p>
        <p class="muted small">Desktop {app?.version ?? '0.1.0'} · Engine {conn.system?.engine.version ?? '—'} · API {conn.system?.engine.api_version ?? '—'}</p>
        <p class="muted small" data-testid="build-stamp">Build {__SARNESS_BUILD__}</p>
        <p class="muted small">Keyboard: Ctrl+1…4 choose Chat/Code/Image/Video in the composer (←/→ also move between modes) · Enter send · Shift+Enter new line · Esc close settings · Ctrl+Shift+O new conversation · Alt+1…5 switch sections · Ctrl+Shift+1…4 focus a pane · Ctrl+Shift+M maximise or restore a pane · Ctrl+B sidebar · in the node editor: Space add node, Ctrl+Z/Y undo/redo, Ctrl+C/V/D copy/paste/duplicate, Ctrl+G group, double-click an edge to add a reroute.</p>
        {#if app}<p class="faint small mono">Config: {app.config_dir}</p>{/if}
      </div>
    {/if}
  </div>
</div>

{#if editing}
  <Modal open={true} title={editing === 'new' ? 'Add server' : `Edit ${editing.name}`} width={760} onclose={() => (editing = null)}>
    <ServerForm profile={editing === 'new' ? null : editing} onsaved={() => (editing = null)} />
  </Modal>
{/if}

<style>
  .settings { display: flex; height: 100%; min-height: 0; }
  nav { width: 240px; flex-shrink: 0; padding: 0 10px; background: var(--bg-1); display: flex; flex-direction: column; gap: 2px; }
  nav h1 { font-size: var(--fs-xl); height: 56px; display: flex; align-items: center; padding: 0 8px; }
  nav button { display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 10px; border: 0; border-radius: var(--r-md); background: none; color: var(--text-1); cursor: pointer; text-align: left; }
  nav button:hover { background: rgba(var(--fg-rgb), 0.045); }
  nav button.on { background: var(--select-bg); color: var(--text-0); }
  nav button :global(svg) { width: 16px; height: 16px; color: var(--text-3); }
  nav button.on :global(svg) { color: var(--lavender); }
  .content { flex: 1; overflow-y: auto; padding: 22px 32px 48px; max-width: 920px; }
  .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .lead { margin-bottom: 18px; line-height: 1.55; }
  .list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 18px; }
  .card { border-radius: var(--r-lg); background: var(--bg-2); border: 1px solid var(--line-1); }
  .prof { display: flex; align-items: center; gap: 12px; padding: 12px 14px; }
  .prof.active { border-color: var(--select-line); }
  .led { width: 9px; height: 9px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }
  .led.on { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .led.off { background: var(--warn); }
  .info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .name { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .sub { font-size: var(--fs-xs); color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .group { display: flex; flex-direction: column; gap: 16px; padding: 18px; margin-bottom: 14px; border-radius: var(--r-lg); background: var(--bg-2); border: 1px solid var(--line-1); }
  .group h3 { font-size: var(--fs-md); color: var(--text-0); }
  .g2 { display: grid; grid-template-columns: 1fr 1.6fr; gap: 12px; }
  .small { font-size: var(--fs-xs); line-height: 1.55; }
  .dir { padding: 6px 10px; border-radius: var(--r-md); background: var(--bg-1); }
  .spacer { flex: 1; }
  .err { color: var(--err); font-size: var(--fs-sm); }
  code { color: var(--lavender); }
</style>
