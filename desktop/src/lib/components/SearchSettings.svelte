<script lang="ts">
  import { api } from '../api/client';
  import type { EngineSettings } from '../api/types';
  import { toastError } from '../stores/toast.svelte';
  import Button from '../ui/Button.svelte';
  import Field from '../ui/Field.svelte';
  import Select from '../ui/Select.svelte';
  import TextInput from '../ui/TextInput.svelte';

  interface Props {
    s: EngineSettings;
    patch: (p: Record<string, unknown>, msg?: string) => Promise<void>;
  }
  let { s, patch }: Props = $props();

  let searxng = $state(s.search_searxng_url ?? '');
  let brave = $state('');
  let test = $state<{ ok: boolean; provider: string; error?: string | null; problem?: string | null; results: { title: string; url: string }[] } | null>(null);
  let busy = $state(false);

  const providers = [
    { value: 'auto', label: 'Automatic', hint: 'SearXNG if its address is set, otherwise DuckDuckGo' },
    { value: 'searxng', label: 'SearXNG (self-hosted)' },
    { value: 'duckduckgo', label: 'DuckDuckGo (no key)' },
    { value: 'brave', label: 'Brave Search (API key)' },
  ];
  const policies = [
    { value: 'manual', label: 'Only when I switch it on in Chat' },
    { value: 'always', label: 'Every Chat message' },
  ];

  async function runTest() {
    busy = true;
    test = null;
    try {
      test = await api.post('/v1/search/test', { query: 'sarness' });
    } catch (e) {
      toastError(e, 'Search test failed');
    } finally {
      busy = false;
    }
  }
</script>

<div class="group">
  <h3>Chat web search</h3>
  <p class="muted small">Used by Chat only (never by the Code agent). The engine runs the query, reads the top pages and passes them to the model as untrusted reference text; answers cite their sources. Pages are fetched from the server, never from this computer.</p>
  <Field label="Provider"><Select value={s.search_provider} options={providers} onchange={(v) => patch({ search_provider: v })} /></Field>
  <Field label="SearXNG address" hint="Base URL of a SearXNG instance with the JSON format enabled, e.g. http://127.0.0.1:8888">
    <div class="row">
      <TextInput bind:value={searxng} placeholder="http://127.0.0.1:8888" mono />
      <Button size="sm" disabled={searxng === (s.search_searxng_url ?? '')} onclick={() => patch({ search_searxng_url: searxng.trim() }, 'Search address saved')}>Save</Button>
    </div>
  </Field>
  <Field label="Brave Search API key" hint={s.search_brave_key ? 'A key is stored on the server. Enter a new one to replace it.' : 'Optional. Stored on the server, never shown again.'}>
    <div class="row">
      <TextInput bind:value={brave} type="password" placeholder={s.search_brave_key ? '•••••••• (stored)' : 'BSA…'} mono />
      <Button size="sm" disabled={!brave} onclick={() => { patch({ search_brave_key: brave }, 'Key saved'); brave = ''; }}>Save</Button>
      {#if s.search_brave_key}<Button size="sm" variant="ghost" onclick={() => patch({ search_brave_key: '' }, 'Key removed')}>Remove</Button>{/if}
    </div>
  </Field>
  <Field label="When to search"><Select value={s.search_policy} options={policies} onchange={(v) => patch({ search_policy: v })} /></Field>
  <div class="row">
    <Button size="sm" onclick={runTest} disabled={busy}>{busy ? 'Testing…' : 'Test search'}</Button>
    {#if test}
      <span class="small {test.ok ? 'faint' : 'err'}">
        {#if test.ok}{test.provider}: {test.results.length} results, e.g. {test.results[0]?.title}
        {:else}{test.provider}: {test.problem ?? test.error ?? 'failed'}{/if}
      </span>
    {/if}
  </div>
</div>

<style>
  .row { display: flex; align-items: center; gap: 8px; }
  .row :global(input) { flex: 1; }
  .err { color: var(--err); }
</style>
