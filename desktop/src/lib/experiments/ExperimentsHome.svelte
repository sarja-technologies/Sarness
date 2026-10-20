<script lang="ts">
  import { ArrowRight, FlaskConical } from '@lucide/svelte';
  import { conn, profileName } from '../stores/connection.svelte';
  import Select from '../ui/Select.svelte';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { PaintSession } from './paintbrush/session.svelte';
  import { EXPERIMENTS, type ClientExperiment } from './registry';
  import { exps, launch, loadCatalogue, setExperimentsEngine, summaryFor } from './store.svelte';

  /** The selection screen shown when entering Experiments: nothing opens until the user picks one. */

  $effect(() => {
    if (conn.state === 'online' && !exps.loaded) loadCatalogue();
  });

  type Avail = { tone: 'ok' | 'warn' | 'neutral' | 'err'; text: string; launchable: boolean };

  function availability(x: ClientExperiment): Avail {
    if (conn.state !== 'online') return { tone: 'neutral', text: 'Connect to your Sarness server to use experiments.', launchable: false };
    if (!exps.loaded) return { tone: 'neutral', text: 'Checking the server…', launchable: false };
    const s = summaryFor(x.id);
    if (!s) {
      return { tone: 'warn', launchable: false,
               text: exps.error ? `The server did not answer: ${exps.error}` : 'The engine on this server does not include this experiment yet — update Sarness Engine.' };
    }
    switch (s.availability.state) {
      case 'ready': return { tone: 'ok', text: `Ready on ${servers.length > 1 ? profileName(exps.engine) : (conn.engines[exps.engine ?? '']?.system?.hardware?.hostname ?? 'your server')}`, launchable: true };
      case 'partial': return { tone: 'ok', text: 'Live preview ready · refine models can be added from inside', launchable: true };
      case 'needs_install': return { tone: 'warn', text: 'Needs a one-time model download on the server', launchable: true };
      default: return { tone: 'err', text: s.availability.reasons.join(' ') || 'Not available on this server', launchable: false };
    }
  }

  // With several servers, experiments run on one of them (sessions and documents live there).
  const servers = $derived(Object.entries(exps.byEngine).map(([id, list]) => ({
    value: id as string | null, label: profileName(id),
    hint: list.some((x) => x.availability.state === 'ready') ? 'ready' : list.length ? 'needs setup' : 'no experiments',
  })));

  const hasDraft = (id: string) => id === 'gods-paintbrush' && PaintSession.hasDraft();
</script>

<div class="home">
  <header>
    <span class="eyebrow"><FlaskConical />Experiments</span>
    <h1>Try what Sarness is working on</h1>
    <p class="muted">Early AI tools that run on your Sarness server's GPU. They keep their own state, so you can switch back to Work at any time.</p>
    {#if servers.length > 1}
      <div class="srv"><span class="faint">Run experiments on</span><Select value={exps.engine} options={servers} size="sm" width="220px" onchange={(v) => v && setExperimentsEngine(v)} /></div>
    {/if}
  </header>

  <div class="cards">
    {#each EXPERIMENTS as x (x.id)}
      {@const a = availability(x)}
      {@const running = exps.launched.includes(x.id)}
      <article class="card">
        {#if x.example}
          <div class="art" aria-hidden="true">
            <img src={x.example.input} alt="" />
            <span class="arrow"><ArrowRight /></span>
            <img src={x.example.output} alt="" />
          </div>
        {/if}
        <div class="content">
          <div class="title">
            <x.icon />
            <h2>{x.name}</h2>
            {#if running}<Badge tone="accent" dot>Open</Badge>{/if}
          </div>
          <p class="tagline">{x.tagline}</p>
          <ul class="facts">
            {#each x.facts as f (f)}<li>{f}</li>{/each}
          </ul>
          {#if x.example}<p class="caption faint">{x.example.caption}</p>{/if}
          <div class="foot">
            <span class="avail {a.tone}">
              {#if conn.state === 'online' && !exps.loaded}<Spinner size={11} />{:else}<span class="led"></span>{/if}
              {a.text}
            </span>
            <Button variant="primary" disabled={!a.launchable} onclick={() => launch(x.id)}>
              {running ? `Resume ${x.name}` : hasDraft(x.id) ? `Continue painting` : x.cta}
            </Button>
          </div>
        </div>
      </article>
    {/each}
  </div>
</div>

<style>
  .srv { display: flex; align-items: center; gap: 10px; margin-top: 12px; font-size: var(--fs-sm); }
  .home { flex: 1; overflow-y: auto; padding: 40px 48px; display: flex; flex-direction: column; gap: 28px; }
  header { display: flex; flex-direction: column; gap: 8px; max-width: 720px; }
  .eyebrow { display: inline-flex; align-items: center; gap: 6px; color: var(--accent); }
  .eyebrow :global(svg) { width: 13px; height: 13px; }
  header p { font-size: var(--fs-lg); }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 560px)); gap: 20px; }
  .card {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--line-1);
    border-radius: var(--r-xl);
    background: var(--bg-0);
    overflow: hidden;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    animation: rise-in var(--t-3) var(--ease-out);
  }
  .art { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 10px; padding: 16px; background: var(--bg-1); border-bottom: 1px solid var(--line-0); }
  .art img { width: 100%; aspect-ratio: 3 / 2; object-fit: cover; border-radius: var(--r-md); box-shadow: 0 0 0 1px var(--line-1); display: block; }
  .arrow { color: var(--text-3); display: grid; place-items: center; }
  .arrow :global(svg) { width: 18px; height: 18px; }
  .content { display: flex; flex-direction: column; gap: 10px; padding: 18px 20px 20px; }
  .title { display: flex; align-items: center; gap: 9px; }
  .title :global(svg) { width: 18px; height: 18px; color: var(--accent); }
  .tagline { font-size: var(--fs-lg); color: var(--text-1); }
  .facts { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
  .facts li { padding: 3px 9px; border-radius: var(--r-full); background: var(--bg-2); font-size: var(--fs-xs); color: var(--text-2); }
  .caption { font-size: var(--fs-xs); }
  .foot { display: flex; align-items: center; gap: 12px; justify-content: space-between; margin-top: 6px; flex-wrap: wrap; }
  .avail { display: flex; align-items: center; gap: 7px; font-size: var(--fs-sm); color: var(--text-2); min-width: 0; flex: 1; }
  .led { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }
  .avail.ok .led { background: var(--ok); }
  .avail.warn .led { background: var(--warn); }
  .avail.err .led { background: var(--err); }
  @media (max-width: 900px) {
    .home { padding: 28px 20px; }
    .cards { grid-template-columns: 1fr; }
  }
</style>
