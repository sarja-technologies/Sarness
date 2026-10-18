<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { onDestroy, tick } from 'svelte';
  import type { Profile } from '../stores/connection.svelte';
  import { saveProfile } from '../stores/connection.svelte';
  import Button from '../ui/Button.svelte';
  import Modal from '../ui/Modal.svelte';
  import Segmented from '../ui/Segmented.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import Badge from '../ui/Badge.svelte';

  let { open = $bindable(), profile, oninstalled }: { open: boolean; profile: Profile; oninstalled: (r: { host: string; port: number; profile: Profile }) => void } = $props();

  let bind = $state<'tailscale' | 'loopback' | 'lan'>('tailscale');
  let withLlama = $state(true);
  let running = $state(false);
  let done = $state(false);
  let error = $state('');
  let lines = $state<{ stage: string; line: string }[]>([]);
  let logEl: HTMLDivElement | undefined = $state();
  let unlisten: (() => void) | null = null;

  onDestroy(() => unlisten?.());

  async function run() {
    running = true;
    error = '';
    lines = [];
    unlisten = await listen<{ stage: string; line: string }>('installer', async (e) => {
      lines.push(e.payload);
      await tick();
      logEl?.scrollTo({ top: logEl.scrollHeight });
    });
    try {
      // Save the profile first so the installer can store the token against it.
      // The installer configures the engine's HTTP API; SNP can be switched on afterwards.
      const saved = await saveProfile({ ...profile, transport: profile.transport === 'https' ? 'https' : 'http', mode: bind === 'loopback' ? 'ssh' : profile.mode });
      const res = await invoke<{ host: string; port: number; data_dir: string; service: string }>('install_engine', {
        profile: saved, options: { bind, install_llama: withLlama, port: profile.port || 8742 },
      });
      // Point the profile at the engine's address.
      const updated: Profile = bind === 'loopback'
        ? { ...saved, mode: 'ssh', host: '127.0.0.1', port: res.port }
        : { ...saved, mode: 'direct', host: res.host, port: res.port };
      const final = await saveProfile(updated);
      done = true;
      oninstalled({ host: res.host, port: res.port, profile: final });
    } catch (e) {
      error = String(e);
    } finally {
      running = false;
      unlisten?.();
      unlisten = null;
    }
  }
</script>

<Modal bind:open title="Install Sarness Engine" subtitle={`on ${profile.ssh?.user}@${profile.ssh?.host} over SSH`} width={720} dismissible={!running}>
  <div class="wiz">
    <ul class="promise">
      <li>Installs into your home directory (<code>~/.local/share/sarness-engine-app</code>) — no sudo.</li>
      <li>Never wipes data, reinstalls the OS, or changes other services or firewalls.</li>
      <li>Runs as a systemd user service; re-running upgrades in place and keeps models and generations.</li>
    </ul>
    <div class="opt">
      <span class="lbl">Listen on</span>
      <Segmented bind:value={bind} items={[
        { value: 'tailscale', label: 'Private network (recommended)' },
        { value: 'loopback', label: 'Localhost + SSH tunnel' },
        { value: 'lan', label: 'Local network address' },
      ]} />
    </div>
    {#if bind === 'tailscale'}<p class="hint">Binds to the server's private-network interface when one is available, otherwise to localhost (then connect through an SSH tunnel).</p>{/if}
    <Toggle bind:checked={withLlama} label="Install llama.cpp (CUDA) for chat" hint="Downloads the official prebuilt llama-server (~770 MB)." />
    {#if lines.length || running}
      <div class="log mono selectable" bind:this={logEl}>
        {#each lines as l, i (i)}<div class="ln {l.stage}">{l.line}</div>{/each}
        {#if running}<div class="ln faint">…</div>{/if}
      </div>
    {/if}
    {#if error}<p class="err selectable">{error}</p>{/if}
    {#if done}<Badge tone="ok" dot>Installed and running — testing the connection</Badge>{/if}
  </div>
  {#snippet footer()}
    <Button variant="ghost" onclick={() => (open = false)} disabled={running}>{done ? 'Close' : 'Cancel'}</Button>
    <Button variant="primary" onclick={run} loading={running} disabled={done}>Install</Button>
  {/snippet}
</Modal>

<style>
  .wiz { display: flex; flex-direction: column; gap: 14px; }
  .promise { margin: 0; padding-left: 18px; color: var(--text-2); display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-sm); }
  code { color: var(--lavender); }
  .opt { display: flex; align-items: center; gap: 12px; }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--text-3); line-height: 1.45; }
  .lbl { font-size: var(--fs-sm); color: var(--text-1); font-weight: 550; }
  .log { height: 260px; overflow-y: auto; padding: 10px 12px; border-radius: var(--r-md); background: var(--bg-0); border: 1px solid var(--line-1); font-size: 11.5px; line-height: 1.55; color: var(--text-1); }
  .ln { white-space: pre-wrap; overflow-wrap: anywhere; }
  .ln.preflight { color: var(--info); }
  .ln.done { color: var(--ok); }
  .err { color: var(--err); font-size: var(--fs-sm); }
</style>
