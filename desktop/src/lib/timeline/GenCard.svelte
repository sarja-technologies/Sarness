<script lang="ts">
  import { Maximize2, TriangleAlert, X } from '@lucide/svelte';
  import { api, qs } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import type { Generation, Job, Page } from '../api/types';
  import { cancelJob, jobs, onGeneration } from '../stores/jobs.svelte';
  import type { GenEntry } from '../stores/sessions';
  import { toastError } from '../stores/toast.svelte';
  import ActionBar from '../studio/ActionBar.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Progress from '../ui/Progress.svelte';
  import Button from '../ui/Button.svelte';
  import { duration } from '../util/format';

  interface Props {
    entry: GenEntry;
    canVary: boolean;
    onrerun: (g: Generation, exact: boolean) => void;
    onvary: (g: Generation, strength: number) => void;
    onedit: (g: Generation) => void;
    onretry: (job: Job) => void;
  }
  let { entry, canVary, onrerun, onvary, onedit, onretry }: Props = $props();

  let gens = $state<Generation[]>([]);
  let missing = $state(false);
  let cancelling = $state(false);
  let lightbox = $state<Generation | null>(null);
  let now = $state(Date.now());

  const job = $derived(jobs.byId[entry.jobId] as Job | undefined);
  const live = $derived(jobs.progress[entry.jobId]);
  const preview = $derived(jobs.previews[entry.jobId]);
  const active = $derived(job?.status === 'queued' || job?.status === 'running');
  const sw = $derived(Number((job?.simple as any)?.width) || 1);
  const sh = $derived(Number((job?.simple as any)?.height) || 1);
  const elapsed = $derived(job?.started_at ? Math.max(0, now / 1000 - job.started_at) : null);

  // A job from a previous run may not be in the live store yet.
  $effect(() => {
    if (job || missing) return;
    api.get<Job>(`/v1/jobs/${entry.jobId}`).then((j) => (jobs.byId[j.id] = j)).catch(() => (missing = true));
  });
  $effect(() => {
    if (job?.status !== 'completed' || gens.length) return;
    api.get<Page<Generation>>(`/v1/generations${qs({ job_id: entry.jobId, limit: 20 })}`).then((p) => (gens = p.items)).catch(() => {});
  });
  $effect(() => onGeneration((g) => {
    if (g.job_id === entry.jobId && !gens.some((x) => x.id === g.id)) gens = [...gens, g];
  }));
  $effect(() => {
    if (job?.status !== 'running') return;
    now = Date.now();
    const t = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(t);
  });

  async function cancel() {
    cancelling = true;
    try {
      await cancelJob(entry.jobId);
    } catch (e) {
      toastError(e, 'Could not cancel');
    } finally {
      cancelling = false;
    }
  }
  const pct = $derived(Math.round((live?.progress ?? job?.progress ?? 0) * 100));
  const stage = $derived(job?.status === 'queued' ? (job.waiting_reason ?? 'Queued') : (live?.text ?? job?.progress_text ?? 'Working…'));
</script>

<svelte:window onkeydown={(e) => { if (lightbox && e.key === 'Escape') lightbox = null; }} />

<div class="card" data-state={job?.status ?? (missing ? 'missing' : 'loading')}>
  {#if missing}
    <p class="note"><TriangleAlert />This {entry.mode} job is no longer on the server. It may have been cleared; check the Library.</p>
  {:else if !job}
    <div class="ph" style="aspect-ratio:1/1"></div>
  {:else if active}
    <div class="stage" style="aspect-ratio:{sw}/{sh}">
      {#if preview}<img class="preview" src={preview} alt="Preview while generating" />{:else}<div class="skeleton fill"></div>{/if}
      <div class="jobbar">
        <div class="jb">
          <div class="row"><span class="jt">{stage}</span><span class="spacer"></span>
            {#if elapsed !== null && job.status === 'running'}<span class="faint tnum">{duration(elapsed)}</span>{/if}
            <span class="tnum muted">{pct}%</span></div>
          <Progress value={job.status === 'queued' ? null : (live?.progress ?? job.progress ?? 0)} />
        </div>
        <button class="cancel" onclick={cancel} disabled={cancelling}>{cancelling ? 'Cancelling…' : 'Cancel'}</button>
      </div>
    </div>
  {:else if job.status === 'failed'}
    <p class="note err"><TriangleAlert />{entry.mode === 'video' ? 'Video' : 'Image'} generation failed: {job.error?.message ?? 'unknown error'}{#if job.error?.hint}<span class="faint"> {job.error.hint}</span>{/if}</p>
    <div class="row"><Button size="sm" variant="secondary" onclick={() => onretry(job)}>Retry</Button></div>
  {:else if job.status === 'cancelled'}
    <p class="note"><X />Cancelled.</p>
    <div class="row"><Button size="sm" variant="secondary" onclick={() => onretry(job)}>Run again</Button></div>
  {:else if gens.length}
    <div class="media" class:multi={gens.length > 1}>
      {#each gens as g (g.id)}
        {#if g.type === 'video'}
          <!-- svelte-ignore a11y_media_has_caption -->
          <video src={mediaUrl(g.url)} poster={mediaUrl(g.thumb_url)} controls preload="metadata" style="aspect-ratio:{g.width ?? 16}/{g.height ?? 9}"></video>
        {:else}
          <button class="imgbtn" onclick={() => (lightbox = g)} aria-label="View full size" style="aspect-ratio:{g.width ?? 1}/{g.height ?? 1}">
            <img src={mediaUrl(g.url)} alt={g.prompt ?? 'Generated image'} loading="lazy" />
            <span class="zoom"><Maximize2 /></span>
          </button>
        {/if}
      {/each}
    </div>
    <ActionBar g={gens[0]} hidePrompt onrerun={(exact) => onrerun(gens[0], exact)} onvary={entry.mode === 'image' && canVary ? (s) => onvary(gens[0], s) : undefined} onedit={() => onedit(gens[0])} />
  {:else}
    <div class="stage" style="aspect-ratio:{sw}/{sh}"><div class="skeleton fill"></div></div>
  {/if}
</div>

{#if lightbox}
  <div class="lightbox" role="dialog" aria-modal="true" aria-label="Full size image" tabindex="-1" onclick={() => (lightbox = null)} onkeydown={() => {}}>
    <img src={mediaUrl(lightbox.url)} alt={lightbox.prompt ?? ''} />
    <div class="lbx"><IconButton label="Close" size="lg" onclick={() => (lightbox = null)}><X /></IconButton></div>
    <div class="lbinfo">{lightbox.width}×{lightbox.height} · seed {lightbox.seed}</div>
  </div>
{/if}

<style>
  .card { display: flex; flex-direction: column; gap: 8px; max-width: min(560px, 100%); }
  .stage { position: relative; width: 100%; max-height: 520px; border-radius: var(--r-lg); overflow: hidden; background: var(--bg-2); border: 1px solid var(--line-1); }
  .ph { width: 160px; border-radius: var(--r-lg); background: var(--bg-2); }
  .fill, .preview { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .preview { filter: saturate(1.05); }
  .jobbar { position: absolute; left: 10px; right: 10px; bottom: 10px; display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: var(--r-md); background: var(--glass-strong); border: 1px solid var(--line-1); backdrop-filter: blur(10px); }
  .jb { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .row { display: flex; align-items: center; gap: 8px; }
  .jt { font-size: var(--fs-sm); color: var(--text-0); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cancel { border: 1px solid var(--line-2); background: var(--bg-0); color: var(--text-0); border-radius: var(--r-sm); height: 26px; padding: 0 10px; font-size: var(--fs-sm); font-weight: 550; cursor: pointer; flex-shrink: 0; }
  .cancel:hover:not(:disabled) { border-color: var(--err); color: var(--err); }
  .cancel:disabled { opacity: 0.6; cursor: default; }
  .note { display: flex; gap: 8px; align-items: flex-start; font-size: var(--fs-sm); color: var(--text-1); padding: 10px 12px; border-radius: var(--r-md); background: var(--bg-2); }
  .note.err { background: var(--err-bg); color: var(--err); }
  .note :global(svg) { width: 15px; height: 15px; flex-shrink: 0; margin-top: 2px; }
  .media { display: grid; gap: 8px; }
  .media.multi { grid-template-columns: 1fr 1fr; }
  .media video, .imgbtn { width: 100%; max-height: 560px; border-radius: var(--r-lg); background: #000; border: 1px solid var(--line-1); display: block; }
  .imgbtn { position: relative; padding: 0; overflow: hidden; cursor: zoom-in; background: var(--bg-2); }
  .imgbtn img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .zoom { position: absolute; top: 8px; right: 8px; display: grid; place-items: center; width: 28px; height: 28px; border-radius: 8px; background: rgba(10, 10, 14, 0.6); color: #fff; opacity: 0; transition: opacity var(--t-2); }
  .zoom :global(svg) { width: 14px; height: 14px; }
  .imgbtn:hover .zoom, .imgbtn:focus-visible .zoom { opacity: 1; }
  .lightbox { position: fixed; inset: 0; z-index: 9000; display: grid; place-items: center; padding: 28px; background: rgba(3, 3, 5, 0.94); animation: fade-in var(--t-2) var(--ease); cursor: zoom-out; }
  .lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--r-md); }
  .lbx { position: absolute; top: 16px; right: 16px; }
  .lbinfo { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); color: var(--text-2); font-size: var(--fs-sm); }
</style>
