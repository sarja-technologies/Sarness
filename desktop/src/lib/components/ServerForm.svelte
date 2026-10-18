<script lang="ts">
  import { CircleCheck, CircleX, FingerprintPattern, KeyRound, Radar, Server, ShieldAlert, ShieldCheck, Terminal } from '@lucide/svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { connect, probePin, saveProfile, testConnection, type Profile, type TestResult } from '../stores/connection.svelte';
  import {
    DEFAULT_HTTP_PORT, DEFAULT_SNP_PORT, TRANSPORTS, describeLimits, describeSession, isValidPin, normalizePin, pinFingerprint,
    transportHint, transportLabel, validateForm,
  } from '../stores/transport';
  import { toast, toastError } from '../stores/toast.svelte';
  import Button from '../ui/Button.svelte';
  import Collapsible from '../ui/Collapsible.svelte';
  import Field from '../ui/Field.svelte';
  import Segmented from '../ui/Segmented.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Badge from '../ui/Badge.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import SystemPanel from './SystemPanel.svelte';
  import InstallWizard from './InstallWizard.svelte';

  let { profile = null, onsaved }: { profile?: Profile | null; onsaved?: (p: Profile) => void } = $props();

  function initialForm(p: Profile | null): Profile {
    const f: Profile = p
      ? (structuredClone($state.snapshot(p)) as Profile)
      : {
          id: '', name: 'AI Server', host: '', port: DEFAULT_HTTP_PORT, scheme: 'http', mode: 'direct',
          ssh: { user: '', host: '', port: 22, key_path: '' }, has_token: false,
          transport: 'snp', snp_port: DEFAULT_SNP_PORT, snp_pin: '', snp_server_name: '',
        };
    f.ssh ??= { user: '', host: '', port: 22, key_path: '' };
    f.transport ??= f.scheme === 'https' ? 'https' : 'http';
    f.snp_port ||= DEFAULT_SNP_PORT;
    f.snp_pin ??= '';
    f.snp_server_name ??= '';
    return f;
  }

  let form = $state<Profile>(initialForm(profile));
  let token = $state('');
  let testing = $state(false);
  let saving = $state(false);
  let result = $state<TestResult | null>(null);
  let discovering = $state(false);
  let candidates = $state<any[] | null>(null);
  let installOpen = $state(false);
  let viaSsh = $state(profile?.mode === 'ssh');
  let caOpen = $state(!!profile?.snp_server_name && !profile?.snp_pin);

  // Trust-on-first-use: the identity the server presented, waiting for the user's explicit confirmation.
  let fetching = $state(false);
  let identityError = $state('');
  let pending = $state<{ pin: string; fingerprint: string; replaces: string } | null>(null);

  const formError = $derived(validateForm({ ...form, snp_pin: form.snp_pin ?? '' }));
  const canDial = $derived(!!form.host.trim() || form.mode === 'ssh');

  function normalized(): Profile {
    const p = $state.snapshot(form) as Profile;
    p.host = p.host.trim().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
    p.port = Number(p.port) || DEFAULT_HTTP_PORT;
    p.snp_port = Number(p.snp_port) || DEFAULT_SNP_PORT;
    p.scheme = p.transport === 'https' ? 'https' : 'http';
    p.snp_pin = normalizePin(p.snp_pin) || null;
    p.snp_server_name = (p.snp_server_name ?? '').trim() || null;
    if (p.mode === 'ssh' && p.ssh) {
      p.ssh.port = Number(p.ssh.port) || 22;
      p.ssh.host = p.ssh.host.trim();
      p.ssh.user = p.ssh.user.trim();
      if (!p.host) p.host = '127.0.0.1';
    } else {
      p.ssh = null;
    }
    return p;
  }

  function setTunnel(on: boolean) {
    viaSsh = on;
    form.mode = on ? 'ssh' : 'direct';
  }

  async function test() {
    testing = true;
    result = null;
    try {
      result = await testConnection(normalized(), token);
    } catch (e) {
      result = { ok: false, stage: 'input', message: String(e), latency_ms: null, health: null, system: null };
    } finally {
      testing = false;
    }
  }

  async function save(andConnect = true) {
    saving = true;
    try {
      const saved = await saveProfile(normalized(), token);
      form.id = saved.id;
      token = '';
      if (andConnect) await connect(saved.id);
      toast('success', andConnect ? `Connected to ${saved.name}` : 'Server saved');
      onsaved?.(saved);
    } catch (e) {
      toastError(e, 'Could not save server');
    } finally {
      saving = false;
    }
  }

  async function discover() {
    discovering = true;
    try {
      candidates = await invoke<any[]>('discover_servers');
    } catch (e) {
      toastError(e, 'Discovery failed');
    } finally {
      discovering = false;
    }
  }

  function pick(c: any) {
    form.host = c.dns_name && c.source === 'tailscale' ? c.dns_name : c.address;
    if (c.source === 'local') form.host = '127.0.0.1';
    form.name = c.source === 'local' ? 'This computer' : c.name;
    setTunnel(false);
    if (!form.ssh!.host) form.ssh!.host = c.dns_name ?? c.address;
  }

  function installed(res: { host: string; port: number; profile: Profile }) {
    form = initialForm(res.profile);
    viaSsh = form.mode === 'ssh';
    installOpen = false;
    token = '';
    test();
  }

  /** Reads the identity the server presents without authenticating. Nothing is saved yet. */
  async function fetchIdentity() {
    fetching = true;
    identityError = '';
    pending = null;
    try {
      const r = await probePin(normalized());
      offerIdentity(r.pin);
    } catch (e) {
      identityError = String(e);
    } finally {
      fetching = false;
    }
  }

  function offerIdentity(pin: string) {
    pending = { pin, fingerprint: pinFingerprint(pin), replaces: normalizePin(form.snp_pin) };
  }

  function acceptIdentity() {
    if (!pending) return;
    form.snp_pin = pending.pin;
    form.snp_server_name = '';
    pending = null;
    result = null;
  }
</script>

<div class="form">
  <div class="discover">
    <div class="row">
      <Radar />
      <div class="col" style="gap:0;flex:1">
        <strong>Find servers</strong>
        <span class="muted small">Checks this computer and the devices your system already knows about for a Sarness Engine. No port scanning.</span>
      </div>
      <Button size="sm" onclick={discover} loading={discovering}>Scan</Button>
    </div>
    {#if candidates}
      <div class="cands">
        {#each candidates.filter((c) => c.online) as c (c.address)}
          <button class="cand" onclick={() => pick(c)}>
            <Server />
            <span class="cname truncate">{c.name}</span>
            <span class="faint truncate mono">{c.dns_name ?? c.address}</span>
            {#if c.engine}<Badge tone="ok" dot>Engine {c.engine.engine_version}</Badge>{:else}<Badge>{c.os ?? 'device'}</Badge>{/if}
          </button>
        {/each}
        {#if !candidates.some((c) => c.online)}<p class="muted small">No online devices found.</p>{/if}
      </div>
    {/if}
  </div>

  <div class="grid2">
    <Field label="Name"><TextInput bind:value={form.name} placeholder="AI Server" /></Field>
    <Field label="Protocol" hint={transportHint(form.transport)}>
      <Segmented bind:value={form.transport} full items={TRANSPORTS.map((t) => ({ value: t.value, label: t.label, title: t.title }))} />
    </Field>
  </div>

  <Toggle checked={viaSsh} onchange={setTunnel} label="Connect through SSH tunnel" hint="Forwards the {transportLabel(form.transport)} port over SSH. Works with every protocol; encryption and sign-in stay end-to-end." />

  {#if viaSsh}
    <div class="grid3">
      <Field label="SSH user"><TextInput bind:value={form.ssh!.user} placeholder="ubuntu" /></Field>
      <Field label="SSH host"><TextInput bind:value={form.ssh!.host} placeholder="ai-server or 192.168.1.50" /></Field>
      <Field label="SSH port"><TextInput bind:value={form.ssh!.port} type="number" /></Field>
    </div>
    <Field label="Private key (optional)" hint="Uses your ssh-agent and ~/.ssh/config by default. Passwords are never stored — Sarness uses key-based SSH only.">
      <TextInput bind:value={form.ssh!.key_path} placeholder="~/.ssh/id_ed25519" mono />
    </Field>
    <div class="grid2">
      <Field label="Engine address on the server" hint="Where the engine listens, as seen from the server."><TextInput bind:value={form.host} placeholder="127.0.0.1" mono /></Field>
      {#if form.transport === 'snp'}
        <Field label="SNP port"><TextInput bind:value={form.snp_port} type="number" /></Field>
      {:else}
        <Field label="Engine port"><TextInput bind:value={form.port} type="number" /></Field>
      {/if}
    </div>
  {:else}
    <div class="grid2 hostport">
      <Field label="Server address" hint="Hostname or IP address of the server (for example 192.168.1.50 or ai-box.example.net), or 127.0.0.1 for this computer.">
        <TextInput bind:value={form.host} placeholder="192.168.1.50" mono />
      </Field>
      {#if form.transport === 'snp'}
        <Field label="SNP port"><TextInput bind:value={form.snp_port} type="number" /></Field>
      {:else}
        <Field label="API port"><TextInput bind:value={form.port} type="number" /></Field>
      {/if}
    </div>
  {/if}

  {#if form.transport === 'snp'}
    <div class="identity">
      <Field
        label="Server identity (pin)"
        hint={form.snp_pin
          ? 'Sarness only talks to a server presenting exactly this identity. A different identity is refused, never silently accepted.'
          : 'Not set yet. Fetch the identity from the server, compare the fingerprint with the one shown on the server, then confirm it.'}
      >
        <div class="pinrow">
          <TextInput bind:value={form.snp_pin} placeholder="sha256:…" mono />
          <Button onclick={fetchIdentity} loading={fetching} disabled={!canDial || fetching}><FingerprintPattern />Fetch identity</Button>
        </div>
      </Field>
      {#if form.snp_pin && !isValidPin(form.snp_pin)}<p class="err small">The identity must look like <code>sha256:</code> followed by 64 hexadecimal digits.</p>{/if}
      {#if identityError}<p class="err small selectable">{identityError}</p>{/if}
      {#if pending}
        <div class="confirm" class:changed={!!pending.replaces && pending.replaces !== pending.pin}>
          <div class="chead">
            {#if pending.replaces && pending.replaces !== pending.pin}<ShieldAlert /><strong>This is a different identity from the saved one</strong>{:else}<ShieldCheck /><strong>Server identity</strong>{/if}
          </div>
          <p class="small">The server at <span class="mono">{form.host || '127.0.0.1'}</span> presents this fingerprint (SHA-256 of its public key):</p>
          <code class="fp selectable">{pending.fingerprint}</code>
          {#if pending.replaces && pending.replaces !== pending.pin}
            <p class="small warn">Saved: <span class="mono selectable">{pending.replaces}</span><br />Only continue if you re-keyed or reinstalled this server. Otherwise something may be impersonating it.</p>
          {:else}
            <p class="small muted">Trust it only if it matches the fingerprint on your server. Nothing was sent to the server except the secure handshake.</p>
          {/if}
          <div class="row">
            <Button variant="primary" size="sm" onclick={acceptIdentity}>Trust this identity</Button>
            <Button variant="ghost" size="sm" onclick={() => (pending = null)}>Cancel</Button>
          </div>
        </div>
      {/if}
      {#if !form.snp_pin}
        <Collapsible title="Server certificate from a certificate authority" bind:open={caOpen}>
          <Field label="Server name" hint="Only for a certificate issued by a certificate authority your system trusts: the name on the certificate. Leave empty when you pin the identity above.">
            <TextInput bind:value={form.snp_server_name} placeholder="ai.example.com" mono />
          </Field>
        </Collapsible>
      {/if}
    </div>
  {/if}

  <Field label="API token" hint={form.has_token ? 'A token is stored in your system keyring. Leave empty to keep it.' : 'Printed by “sarness-engine init” on the server. Stored in your system keyring, never in plain config.'}>
    <TextInput bind:value={token} type="password" placeholder={form.has_token ? '••••••••  (stored)' : 'sk-sarness-…'} mono>
      {#snippet icon()}<KeyRound />{/snippet}
    </TextInput>
  </Field>

  {#if formError && form.host.trim()}<p class="err small">{formError}</p>{/if}

  <div class="actions">
    {#if viaSsh}
      <Button variant="ghost" onclick={() => (installOpen = true)} disabled={!form.ssh?.user || !form.ssh?.host}><Terminal />Install engine via SSH…</Button>
    {/if}
    <div class="spacer"></div>
    <Button onclick={test} loading={testing} disabled={!canDial || !!formError}>Test connection</Button>
    <Button variant="primary" onclick={() => save(true)} loading={saving} disabled={!canDial || !!formError || (!token && !form.has_token)}>Save & connect</Button>
  </div>

  {#if testing}
    <div class="result pending"><Spinner /> Testing connection…</div>
  {:else if result}
    <div class="result" class:ok={result.ok} class:bad={!result.ok}>
      <div class="rhead">
        {#if result.ok}<CircleCheck />{:else}<CircleX />{/if}
        <strong>{result.ok ? (result.transport === 'snp' ? 'Connected over SNP' : 'Connected') : 'Connection failed'}</strong>
        {#if result.latency_ms !== null}<span class="faint tnum">{result.latency_ms} ms</span>{/if}
        {#if result.health}<Badge>Engine {String(result.health.engine_version)} · API {String(result.health.api_version)}</Badge>{/if}
      </div>
      {#if !result.ok}
        <p class="msg selectable">{result.message}</p>
        {#if result.error_kind === 'pin_mismatch' && result.expected_pin && result.presented_pin}
          <div class="pins">
            <span class="faint small">Saved identity</span><code class="mono selectable">{result.expected_pin}</code>
            <span class="faint small">Presented by the server</span><code class="mono selectable">{result.presented_pin}</code>
          </div>
          <div class="row"><Button size="sm" onclick={() => offerIdentity(result!.presented_pin!)}><ShieldAlert />Review the new identity…</Button></div>
        {:else if result.error_kind === 'untrusted_certificate' && result.presented_pin}
          <div class="row"><Button size="sm" onclick={() => offerIdentity(result!.presented_pin!)}><FingerprintPattern />Review this identity…</Button></div>
        {/if}
      {/if}
      {#if result.ok && result.snp}
        <div class="chips">
          <Badge tone="accent">SNP v{result.snp.version}</Badge>
          <Badge>handshake {result.snp.handshake_ms} ms</Badge>
          {#each result.snp.features as f (f)}<Badge>{f}</Badge>{/each}
        </div>
        <p class="small muted selectable">{describeSession(result.snp)}<br />{describeLimits(result.snp.limits)}</p>
        <p class="small faint selectable">Identity <span class="mono">{result.snp.fingerprint}</span></p>
      {/if}
      {#if result.system}
        <SystemPanel system={result.system} />
        <div class="chips">
          <Badge tone="accent">{result.system.models.installed} models installed</Badge>
          <Badge>{result.system.jobs.running ?? 0} running · {result.system.jobs.queued ?? 0} queued</Badge>
          <Badge tone={result.system.health.status === 'ok' ? 'ok' : 'warn'} dot>Health: {result.system.health.status}</Badge>
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if installOpen}
  <InstallWizard bind:open={installOpen} profile={normalized()} oninstalled={installed} />
{/if}

<style>
  .form { display: flex; flex-direction: column; gap: 16px; }
  .discover { padding: 12px 14px; border-radius: var(--r-lg); background: var(--bg-1); border: 1px solid var(--line-1); display: flex; flex-direction: column; gap: 10px; }
  .discover > .row :global(svg) { width: 18px; height: 18px; color: var(--lavender); }
  .small { font-size: var(--fs-sm); }
  .cands { display: flex; flex-direction: column; gap: 4px; }
  .cand { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: var(--r-md); border: 1px solid transparent; background: var(--bg-2); color: var(--text-1); cursor: pointer; text-align: left; }
  .cand:hover { border-color: var(--select-line); background: var(--select-bg); }
  .cand :global(svg) { width: 15px; height: 15px; color: var(--text-3); flex-shrink: 0; }
  .cname { font-weight: 600; color: var(--text-0); min-width: 100px; }
  .cand .mono { flex: 1; font-size: var(--fs-xs); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .hostport { grid-template-columns: 1fr 140px; }
  .grid3 { display: grid; grid-template-columns: 1fr 1.4fr 110px; gap: 12px; }
  .identity { display: flex; flex-direction: column; gap: 10px; padding: 12px 14px; border-radius: var(--r-lg); background: var(--bg-1); border: 1px solid var(--line-1); }
  .pinrow { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
  .confirm { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-radius: var(--r-md); background: var(--bg-2); border: 1px solid var(--line-1); }
  .confirm.changed { border-color: rgba(242, 182, 90, 0.5); }
  .chead { display: flex; align-items: center; gap: 8px; }
  .chead :global(svg) { width: 17px; height: 17px; color: var(--ok); }
  .changed .chead :global(svg) { color: var(--warn); }
  .fp { display: block; padding: 8px 10px; border-radius: var(--r-sm); background: var(--bg-0); font-family: var(--font-mono, monospace); font-size: var(--fs-xs); line-height: 1.6; word-break: break-all; color: var(--text-0); }
  .warn { color: var(--warn); }
  .err { color: var(--err); }
  .pins { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; align-items: baseline; }
  .pins code { font-size: var(--fs-xs); word-break: break-all; }
  .actions { display: flex; gap: 8px; align-items: center; }
  .spacer { flex: 1; }
  .result { padding: 14px; border-radius: var(--r-lg); border: 1px solid var(--line-1); background: var(--bg-1); display: flex; flex-direction: column; gap: 12px; animation: rise-in var(--t-3) var(--ease-out); }
  .pending { flex-direction: row; align-items: center; gap: 10px; color: var(--text-2); }
  .ok { border-color: rgba(95, 211, 155, 0.3); }
  .bad { border-color: rgba(244, 113, 116, 0.3); }
  .rhead { display: flex; align-items: center; gap: 8px; }
  .rhead :global(svg) { width: 18px; height: 18px; }
  .ok .rhead :global(svg) { color: var(--ok); }
  .bad .rhead :global(svg) { color: var(--err); }
  .msg { color: var(--text-1); line-height: 1.5; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
</style>
