<script lang="ts">
  import { Cpu, ListTodo, MessageSquareText, Network, RefreshCw, Server, ShieldCheck } from '@lucide/svelte';
  import { activeProfile, conn, engineList, profileById, profileName, reconnectNow, type EngineConn } from '../stores/connection.svelte';
  import { activeJobs } from '../stores/jobs.svelte';
  import { models } from '../stores/models.svelte';
  import { prefs, persistPrefs } from '../stores/prefs.svelte';
  import { mib } from '../util/format';
  import { describeLimits, describeSession, shortPin, transportLabel } from '../stores/transport';
  import { tooltip } from '../ui/tooltip';

  const gpu = $derived(conn.stats?.gpus?.[0] ?? conn.system?.hardware.gpus?.[0] ?? null);
  const gpuName = $derived(conn.system?.hardware.gpus?.[0]?.name?.replace('NVIDIA GeForce ', '') ?? null);
  const vramPct = $derived(gpu ? gpu.vram_used_mb / gpu.vram_total_mb : 0);
  const running = $derived(activeJobs().filter((j) => j.status === 'running').length);
  const queued = $derived(activeJobs().filter((j) => j.status === 'queued').length);
  const llm = $derived(models.llm);
  // Several servers: one compact segment each (state, memory), details in the tooltip.
  const engines = $derived(engineList());
  const multi = $derived(engines.length > 1);
  const llms = $derived(engines.map((e) => ({ id: e.id, llm: models.by[e.id]?.llm })).filter((x) => x.llm && x.llm.state !== 'stopped'));
  function engineGpu(e: EngineConn) {
    return e.stats?.gpus?.[0] ?? e.system?.hardware.gpus?.[0] ?? null;
  }
  function engineTip(e: EngineConn): string {
    const g = engineGpu(e);
    const name = e.system?.hardware.gpus?.[0]?.name ?? e.caps?.gpu?.name;
    const unified = e.caps?.gpu?.unified_memory;
    const st = { online: 'Connected', offline: 'Offline — reconnecting', connecting: 'Connecting…' }[e.state];
    return [
      `${profileName(e.id)}${e.id === conn.activeId ? ' (primary)' : ''} · ${st}`,
      e.lastError && e.state !== 'online' ? e.lastError : '',
      name ? `${name}${g ? ` · ${unified ? 'memory' : 'VRAM'} ${mib(g.vram_used_mb)} of ${mib(g.vram_total_mb)}${g.utilization_pct != null ? ` · GPU ${g.utilization_pct}%` : ''}` : ''}` : '',
      e.transport ? `${transportLabel(e.transport)}${profileById(e.id)?.mode === 'ssh' ? ' via SSH' : ''}${e.system ? ` · engine ${e.system.engine.version}` : ''}` : '',
    ].filter(Boolean).join('\n');
  }
  const via = $derived(activeProfile()?.mode === 'ssh' ? ' through an SSH tunnel' : '');
  const transportTip = $derived.by(() => {
    if (!conn.transport) return '';
    const s = conn.snp?.session;
    if (conn.transport === 'snp') {
      if (!s) return `SNP${via} - ${conn.snp?.status.error ?? 'negotiating'}`;
      return `${describeSession(s)} - ${describeLimits(s.limits)} - identity ${shortPin(s.pin)}${via}`;
    }
    return `${transportLabel(conn.transport)} connection to the API${via}`;
  });
  const stateLabel = $derived(
    { online: 'Connected', offline: 'Offline — reconnecting', connecting: 'Connecting…', booting: '', unconfigured: 'No server' }[conn.state],
  );
</script>

<footer class="status">
  {#if multi}
    {#each engines as e (e.id)}
      {@const g = engineGpu(e)}
      <button class="seg conn {e.state}" onclick={() => e.state === 'offline' && reconnectNow(e.id)} use:tooltip={engineTip(e)}>
        <span class="led"></span>
        <Server />
        <span class="truncate">{profileName(e.id)}</span>
        {#if g && e.state === 'online'}<span class="bar"><span style="transform:scaleX({g.vram_used_mb / g.vram_total_mb})" class:hot={g.vram_used_mb / g.vram_total_mb > 0.9}></span></span>{/if}
        {#if e.state === 'offline'}<RefreshCw class="spin" />{/if}
      </button>
    {/each}
  {:else}
  <button class="seg conn {conn.state}" onclick={() => conn.state === 'offline' && reconnectNow()} use:tooltip={conn.lastError ?? stateLabel}>
    <span class="led"></span>
    <Server />
    <span class="truncate">{activeProfile()?.name ?? 'Server'}</span>
    <span class="faint">· {stateLabel}</span>
    {#if conn.state === 'offline'}<RefreshCw class="spin" />{/if}
  </button>

  {#if conn.transport && conn.state !== 'unconfigured' && conn.state !== 'booting'}
    <span class="seg tp {conn.transport}" use:tooltip={transportTip}>
      {#if conn.transport === 'http'}<Network />{:else}<ShieldCheck />{/if}
      <span>{transportLabel(conn.transport)}</span>{#if activeProfile()?.mode === 'ssh'}<span class="faint">via SSH</span>{/if}
    </span>
  {/if}

  {#if gpu && conn.state === 'online'}
    <div class="seg" use:tooltip={`VRAM ${mib(gpu.vram_used_mb)} of ${mib(gpu.vram_total_mb)} · GPU ${gpu.utilization_pct}%${gpu.temperature_c ? ` · ${gpu.temperature_c}°C` : ''}`}>
      <Cpu />
      <span>{gpuName}</span>
      <span class="bar"><span style="transform:scaleX({vramPct})" class:hot={vramPct > 0.9}></span></span>
      <span class="tnum faint">{mib(gpu.vram_used_mb)} / {mib(gpu.vram_total_mb)}</span>
      <span class="tnum faint">{gpu.utilization_pct}%</span>
    </div>
  {/if}
  {/if}

  <div class="spacer"></div>

  {#if multi}
    {#each llms as x (x.id)}
      <button class="seg link" onclick={() => { prefs.view = 'workspace'; persistPrefs(); }} use:tooltip={x.llm?.error ?? `Chat model ${x.llm?.state} on ${profileName(x.id)}`}>
        <MessageSquareText />
        <span class="truncate" style="max-width:180px">{x.llm?.model_name ?? x.llm?.model_id}</span>
        <span class="pill {x.llm?.state}">{x.llm?.state === 'ready' ? (x.llm.active_streams ? 'generating' : 'loaded') : x.llm?.state}</span>
      </button>
    {/each}
  {:else if llm && llm.state !== 'stopped'}
    <button class="seg link" onclick={() => { prefs.view = 'workspace'; persistPrefs(); }} use:tooltip={llm.error ?? `Chat model ${llm.state}`}>
      <MessageSquareText />
      <span class="truncate" style="max-width:220px">{llm.model_name ?? llm.model_id}</span>
      <span class="pill {llm.state}">{llm.state === 'ready' ? (llm.active_streams ? 'generating' : 'loaded') : llm.state}</span>
    </button>
  {/if}

  <button class="seg link" onclick={() => { prefs.view = 'system'; persistPrefs(); }} use:tooltip={'Generation queue and server dashboard'}>
    <ListTodo />
    {#if running || queued}
      <span>{running} running{queued ? ` · ${queued} queued` : ''}</span>
    {:else}
      <span class="faint">Queue idle</span>
    {/if}
  </button>
  {#if conn.system && !multi}<span class="seg faint">Engine {conn.system.engine.version}</span>{/if}
</footer>

<style>
  .status {
    grid-column: 2;
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 0 8px;
    background: var(--bg-0);
    border-left: 1px solid var(--line-0);
    font-size: var(--fs-xs);
    color: var(--text-2);
    min-width: 0;
    overflow: hidden;
  }
  .seg {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 22px;
    padding: 0 8px;
    border: 0;
    border-radius: 6px;
    background: none;
    color: inherit;
    font-size: inherit;
    white-space: nowrap;
    min-width: 0;
  }
  button.seg { cursor: pointer; }
  button.seg:hover { background: rgba(var(--fg-rgb), 0.05); color: var(--text-0); }
  .seg :global(svg) { width: 13px; height: 13px; flex-shrink: 0; }
  .led { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }
  .online .led { background: var(--ok); box-shadow: 0 0 8px rgba(95, 211, 155, 0.7); }
  .offline .led { background: var(--warn); box-shadow: 0 0 8px rgba(242, 182, 90, 0.6); }
  .connecting .led { background: var(--lavender); animation: pulse-ring 1.4s infinite; }
  .tp.snp :global(svg), .tp.https :global(svg) { color: var(--ok); }
  .bar { width: 64px; height: 4px; border-radius: 3px; background: rgba(var(--fg-rgb), 0.08); overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent-hi); transform-origin: left; transition: transform 600ms var(--ease-out); }
  .bar span.hot { background: var(--warn); }
  .pill { padding: 0 6px; border-radius: 8px; background: rgba(var(--fg-rgb), 0.07); font-weight: 600; }
  .pill.ready { color: var(--ok); }
  .pill.loading { color: var(--lavender); }
  .pill.error { color: var(--err); }
  .spacer { flex: 1; }
  :global(.spin) { animation: spin 1.2s linear infinite; }
</style>
