<script lang="ts">
  import { Crown, KeyRound, Pencil, Plug, ShieldCheck, Trash2, Unplug } from '@lucide/svelte';
  import { conn, connect, disconnect, saveProfile, setPrimary, type Profile } from '../stores/connection.svelte';
  import { models } from '../stores/models.svelte';
  import { toastError } from '../stores/toast.svelte';
  import { endpointLabel, profileTransport, shortPin, transportLabel } from '../stores/transport';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import { mib } from '../util/format';

  /** One server profile: connection, identity, hardware, models, load and preferred workloads. */
  let { p, onedit, onremove }: { p: Profile; onedit: () => void; onremove: () => void } = $props();

  const TASKS = [
    { id: 'chat', label: 'Chat' },
    { id: 'code', label: 'Code' },
    { id: 'vision', label: 'Vision' },
    { id: 'image', label: 'Image' },
    { id: 'video', label: 'Video' },
  ];

  const e = $derived(conn.engines[p.id]);
  const caps = $derived(e?.caps ?? null);
  const gpu = $derived(caps?.gpu ?? null);
  const primary = $derived(conn.activeId === p.id && !!e);
  const mine = $derived(models.by[p.id]);
  const counts = $derived.by(() => {
    const list = mine?.installed.filter((m) => m.supported) ?? [];
    const n = (c: string) => list.filter((m) => m.category === c).length;
    return [['LLM', n('llm')], ['image', n('image')], ['video', n('video')], ['other', list.length - n('llm') - n('image') - n('video')]].filter(([, v]) => v) as [string, number][];
  });
  const llm = $derived(mine?.llm ?? null);
  const preferred = $derived(new Set(p.preferred_tasks ?? []));
  let busy = $state(false);

  async function run(f: () => Promise<unknown>) {
    busy = true;
    try {
      await f();
    } catch (err) {
      toastError(err);
    } finally {
      busy = false;
    }
  }

  function togglePreferred(task: string) {
    const next = preferred.has(task) ? [...preferred].filter((t) => t !== task) : [...preferred, task];
    void run(() => saveProfile({ ...$state.snapshot(p), preferred_tasks: next } as Profile));
  }
</script>

<div class="card srv" class:primary>
  <div class="top">
    <span class="led" class:on={e?.state === 'online'} class:off={e?.state === 'offline'} class:wait={e?.state === 'connecting'}></span>
    <div class="info">
      <div class="name">
        {p.name}
        <Badge tone={profileTransport(p) === 'snp' ? 'accent' : 'neutral'}>{transportLabel(profileTransport(p))}</Badge>
        {#if primary}<Badge tone="accent"><Crown />primary</Badge>{/if}
        {#if e}<Badge tone={e.state === 'online' ? 'ok' : 'warn'}>{e.state}</Badge>{/if}
        {#if caps && caps.health.status !== 'ok'}<Badge tone="warn">degraded</Badge>{/if}
      </div>
      <div class="sub mono">{endpointLabel(p)}</div>
    </div>
    {#if !p.has_token}<Badge tone="warn"><KeyRound />No token</Badge>{/if}
    {#if e && e.state !== 'offline'}
      {#if !primary}<Button size="sm" variant="ghost" disabled={busy} onclick={() => run(() => setPrimary(p.id))} title="Use this server for new work by default and for projects">Make primary</Button>{/if}
      <Button size="sm" variant="ghost" disabled={busy} onclick={() => run(() => disconnect(p.id))}><Unplug />Disconnect</Button>
    {:else}
      <Button size="sm" variant="subtle" disabled={busy} onclick={() => run(() => connect(p.id))}><Plug />{e ? 'Reconnect' : 'Connect'}</Button>
      {#if e}<Button size="sm" variant="ghost" disabled={busy} onclick={() => run(() => disconnect(p.id))}><Unplug />Disconnect</Button>{/if}
    {/if}
    <IconButton label="Edit" onclick={onedit}><Pencil /></IconButton>
    <IconButton label="Remove" tone="danger" onclick={onremove}><Trash2 /></IconButton>
  </div>

  {#if e?.state === 'online' && caps}
    <dl class="facts">
      <dt>Hardware</dt>
      <dd>
        {gpu?.name ?? 'No NVIDIA GPU'}{#if gpu?.compute_capability} · sm {gpu.compute_capability}{/if}{#if caps.arch} · {caps.arch}{/if}
        <span class="faint">· {caps.cpu}</span>
      </dd>
      <dt>Memory</dt>
      <dd>
        {#if gpu?.unified_memory}
          {mib(caps.memory.available_mb)} free of {mib(caps.memory.total_mb)} <span class="faint">unified (shared by CPU and GPU)</span>
        {:else if gpu}
          {mib(gpu.memory_free_mb)} VRAM free of {mib(gpu.memory_total_mb)} · {mib(caps.memory.available_mb)} RAM free
        {:else}
          {mib(caps.memory.available_mb)} RAM free of {mib(caps.memory.total_mb)}
        {/if}
        <span class="faint">· {Math.round(caps.storage_free_gb)} GB disk free</span>
      </dd>
      <dt>Models</dt>
      <dd>{#if counts.length}{counts.map(([k, v]) => `${v} ${k}`).join(' · ')}{:else}<span class="faint">none installed</span>{/if}</dd>
      <dt>Workload</dt>
      <dd>
        {#if llm && llm.state !== 'stopped'}{llm.model_name ?? llm.model_id} {llm.state}{llm.active_streams ? ` · ${llm.active_streams} generating` : ''} · {:else}no chat model loaded · {/if}
        {caps.jobs.running} running · {caps.jobs.queued} queued{#if caps.jobs.busy_reason} <span class="faint">({caps.jobs.busy_reason})</span>{/if}
      </dd>
      <dt>Engine</dt>
      <dd>
        {caps.engine_version} · API {caps.api_version}{#if caps.backends.length} · {caps.backends.join(', ')}{/if}
        {#if caps.derived}<span class="faint">· older engine (limited server information)</span>{/if}
      </dd>
      <dt>Identity</dt>
      <dd class="mono small">
        {#if caps.server_id}{caps.server_id}{:else}<span class="faint">not reported</span>{/if}
        {#if e.snp?.session}<span class="pin"><ShieldCheck />{shortPin(e.snp.session.pin)}</span>{:else if profileTransport(p) === 'snp'}<span class="faint"> · SNP negotiating</span>{:else}<span class="faint"> · token over {transportLabel(profileTransport(p))}</span>{/if}
      </dd>
      {#if caps.health.issues.length}<dt>Issues</dt><dd class="warn">{caps.health.issues.join('; ')}</dd>{/if}
    </dl>
  {:else if e?.lastError}
    <p class="err selectable">{e.lastError}</p>
  {/if}

  <div class="pref">
    <span class="faint small">Preferred for</span>
    {#each TASKS as t (t.id)}
      <button type="button" class="chip" class:on={preferred.has(t.id)} aria-pressed={preferred.has(t.id)} disabled={busy} onclick={() => togglePreferred(t.id)}>{t.label}</button>
    {/each}
  </div>
</div>

<style>
  .srv { display: flex; flex-direction: column; gap: 10px; padding: 12px 14px; }
  .srv.primary { border-color: var(--select-line); }
  .top { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .info { flex: 1; min-width: 0; }
  .name { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-weight: 600; }
  .name :global(svg) { width: 11px; height: 11px; }
  .sub { font-size: var(--fs-xs); color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .led { width: 8px; height: 8px; border-radius: 50%; background: var(--line-2); flex-shrink: 0; }
  .led.on { background: var(--ok); }
  .led.off { background: var(--warn); }
  .led.wait { background: var(--accent); }
  .facts { display: grid; grid-template-columns: 92px 1fr; gap: 4px 12px; margin: 0; padding: 10px 0 0 16px; border-top: 1px solid var(--line-0); font-size: var(--fs-sm); }
  dt { color: var(--text-2); }
  dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
  .pin { display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; color: var(--text-1); }
  .pin :global(svg) { width: 12px; height: 12px; color: var(--ok); }
  .warn { color: var(--warn); }
  .err { margin: 0 0 0 16px; font-size: var(--fs-sm); color: var(--err); }
  .pref { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding-left: 16px; }
  .chip { height: 24px; padding: 0 10px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-1); font-size: var(--fs-xs); cursor: pointer; }
  .chip.on { background: var(--select-bg); border-color: var(--select-line); color: var(--text-0); }
  .chip:disabled { opacity: 0.6; cursor: default; }
  .small { font-size: var(--fs-xs); }
</style>
