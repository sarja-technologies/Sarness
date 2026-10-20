<script lang="ts">
  import { conn, engineList, profileName } from '../stores/connection.svelte';
  import type { RouteResult } from '../stores/router';
  import Field from '../ui/Field.svelte';
  import Select from '../ui/Select.svelte';

  /**
   * Where this conversation's work runs: automatic routing, or one server chosen by hand. Shown only
   * when several servers are connected (or a chosen server is no longer connected).
   */
  let { route = $bindable(), result }: { route: string | null; result: RouteResult | null } = $props();

  const engines = $derived(engineList());
  const visible = $derived(engines.length > 1 || (!!route && !conn.engines[route]));
  const options = $derived([
    { value: null as string | null, label: 'Automatic', hint: result?.ok && !route ? `now: ${profileName(result.engine)} (${result.reason})` : 'by model, availability and load' },
    ...engines.map((e) => ({ value: e.id as string | null, label: profileName(e.id), hint: e.state === 'online' ? (e.caps?.gpu?.name ?? '') : 'offline' })),
    ...(route && !conn.engines[route] ? [{ value: route as string | null, label: profileName(route), hint: 'not connected' }] : []),
  ]);
  const hint = $derived(result && !result.ok ? '' : route ? 'All new work in this conversation runs here. Running work is never moved.' : 'Each request goes to a server that has the chosen model.');
</script>

{#if visible}
  <div class="server">
    <Field label="Server" {hint} error={result && !result.ok ? result.error : ''}>
      <Select bind:value={route} {options} size="sm" />
    </Field>
  </div>
{/if}

<style>
  .server { margin-bottom: 14px; }
</style>
