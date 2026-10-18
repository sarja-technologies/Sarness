<script lang="ts">
  import { AlertTriangle, CheckCircle2, Code2, OctagonX, Play, RotateCcw } from '@lucide/svelte';
  import { renderMarkdown } from '../../chat/markdown';
  import Button from '../../ui/Button.svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import { currentPhase, summarize } from '../reduce';
  import type { AgentRun } from '../types';
  import PlanCard from './PlanCard.svelte';
  import ToolCard from './ToolCard.svelte';

  interface Props {
    run: AgentRun;
    isLast: boolean;
    running: boolean;
    canContinue: boolean;
    onapprove: (approvalId: string, ok: boolean) => void;
    oncontinue: () => void;
    onchanges: (path?: string) => void;
  }
  let { run, isLast, running, canContinue, onapprove, oncontinue, onchanges }: Props = $props();
  const phase = $derived(currentPhase(run));
  const sum = $derived(summarize(run));
  const live = $derived(run.status === 'running');
  const byCommand = $derived.by(() => {
    const known = new Set(sum.files.map((f) => f.path));
    const d = run.disk;
    return d ? [...d.created.map((p) => `${p} (new)`), ...d.modified, ...d.deleted.map((p) => `${p} (deleted)`)].filter((l) => !known.has(l.replace(/ \((new|deleted)\)$/, ''))) : [];
  });
  const failedCmds = $derived(sum.commands.filter((c) => !c.ok && !c.test).length);
  const statusLabel = $derived({ completed: 'Completed', cancelled: 'Stopped', blocked: 'Blocked', error: 'Failed', running: phase }[run.status]);
</script>

<section class="run" aria-label="Coding task">
  <div class="gprompt"><div class="bubble selectable">{run.prompt}</div></div>

  <div class="status" class:live data-status={run.status} role="status" aria-live="polite">
    {#if live}<Spinner size={13} />{:else if run.status === 'completed'}<CheckCircle2 class="ok" />{:else if run.status === 'cancelled'}<OctagonX class="dim" />{:else}<AlertTriangle class="warn" />{/if}
    <strong>{statusLabel}</strong>{#if live && run.drafting}<span class="faint">writing {run.drafting.replace(/_/g, ' ')}…</span>{/if}
    <span class="faint">{run.permission === 'ask' ? 'Ask' : run.permission === 'full_access' ? 'Full Access' : 'Workspace Auto'}</span>
  </div>

  <div class="items">
    {#each run.items as it (it.id)}
      {#if it.kind === 'text'}
        {#if it.text.trim() || it.reasoning}
          <div class="said selectable" class:final={it.final}>
            {#if it.reasoning && !it.final && live}<details class="think"><summary>Thinking</summary><pre>{it.reasoning}</pre></details>{/if}
            {#if it.text.trim()}<div class="md">{@html renderMarkdown(it.text)}</div>{/if}
          </div>
        {/if}
      {:else if it.kind === 'tool'}
        {#if it.name !== 'update_plan'}<ToolCard item={it} {onapprove} />{/if}
      {:else if it.kind === 'plan'}
        <PlanCard steps={it.steps} />
      {:else}
        <div class="notice {it.level}" role={it.level === 'info' ? undefined : 'alert'}>{it.text}</div>
      {/if}
    {/each}
  </div>

  {#if !live}
    {#if run.status === 'blocked' || run.status === 'error' || run.status === 'cancelled'}
      <div class="banner {run.status}">
        <div>
          {#if run.status === 'cancelled'}<strong>Stopped.</strong> Nothing further runs. Files already changed stay changed; review them below.
          {:else if run.status === 'blocked'}<strong>Blocked.</strong> {run.reason}
          {:else}<strong>The task failed.</strong> {run.reason}{/if}
        </div>
        {#if isLast && canContinue}
          <Button size="sm" variant="primary" onclick={oncontinue} disabled={running}>
            {#if run.status === 'error'}<RotateCcw />Retry{:else}<Play />Continue task{/if}
          </Button>
        {/if}
      </div>
    {/if}

    <div class="summary" aria-label="Task summary">
      <div class="sh">
        <strong>Summary</strong>
        <span class="vf {sum.verification}">
          {sum.verification === 'passed' ? 'Last build/test command exited 0' : sum.verification === 'failed' ? 'Last build/test command failed' : sum.verification === 'stale' ? 'Files changed after the last passing command' : 'Nothing was built or tested'}
        </span>
      </div>
      {#if run.status === 'completed' && sum.verification === 'failed'}
        <div class="warnbox" role="alert"><strong>Do not trust a “done” claim here:</strong> the agent finished, but the last build/test command it ran exited with an error. Review the failing output above.</div>
      {/if}
      {#if run.disk?.empty.length}
        <div class="warnbox" role="alert">⚠ Empty file{run.disk.empty.length === 1 ? '' : 's'} on disk: <code>{run.disk.empty.slice(0, 6).join(', ')}</code>. A command that exits 0 does not prove an empty file works; check the agent’s claims.</div>
      {/if}
      {#if sum.files.length}
        <ul class="files">
          {#each sum.files as f (f.path)}
            <li><button type="button" class="fbtn" onclick={() => onchanges(f.path)} title="View changes"><Code2 /><span class="truncate">{f.path}</span>
              <span class="pm">{#if f.deleted}deleted{:else if f.moved}moved from {f.moved}{:else}{#if f.created}new · {/if}<em class="a">+{f.added}</em> <em class="r">−{f.removed}</em>{/if}</span></button></li>
          {/each}
        </ul>
      {/if}
      {#if byCommand.length}
        <p class="faint small">Also changed on disk (by commands, not file tools): {byCommand.slice(0, 8).join(', ')}{byCommand.length > 8 ? ` and ${byCommand.length - 8} more` : ''}{run.disk?.truncated ? ' (project too large to scan completely)' : ''}.</p>
      {/if}
      {#if !sum.files.length && !byCommand.length}
        <p class="faint small">No files were changed.</p>
      {/if}
      {#if sum.commands.length}
        <ul class="cmds">
          {#each sum.commands.slice(-8) as c, i (i)}
            <li class:bad={!c.ok}><span>{c.ok ? '✓' : '✗'}</span><code class="truncate">{c.command}</code><span class="faint">exit {c.exit ?? '–'}</span></li>
          {/each}
        </ul>
        {#if failedCmds}<p class="faint small">{failedCmds} command{failedCmds === 1 ? '' : 's'} exited with an error along the way.</p>{/if}
      {/if}
      <p class="faint small">Sarness reports real exit codes. It cannot judge whether the tests themselves are meaningful, so review the diffs.</p>
      <div class="sacts"><Button size="sm" variant="ghost" onclick={() => onchanges()}>Review all changes</Button></div>
    </div>
  {/if}
</section>

<style>
  .run { display: flex; flex-direction: column; gap: 10px; }
  .gprompt { display: flex; justify-content: flex-end; }
  .bubble { max-width: 80%; padding: 9px 14px; border-radius: 18px 18px 4px 18px; background: var(--select-bg); border: 1px solid var(--select-line); white-space: pre-wrap; word-break: break-word; }
  .status { display: inline-flex; align-items: center; gap: 8px; font-size: var(--fs-sm); color: var(--text-1); }
  .status :global(svg) { width: 15px; height: 15px; }
  .status :global(.ok) { color: var(--ok); }
  .status :global(.warn) { color: var(--warn); }
  .status :global(.dim) { color: var(--text-3); }
  .faint { color: var(--text-3); font-size: var(--fs-xs); }
  .small { margin: 4px 0 0; }
  .items { display: flex; flex-direction: column; gap: 8px; }
  .said { color: var(--text-0); line-height: 1.55; }
  .said.final { padding: 2px 0; }
  .md :global(p) { margin: 0 0 8px; }
  .think { font-size: var(--fs-xs); color: var(--text-3); margin-bottom: 4px; }
  .think pre { white-space: pre-wrap; margin: 4px 0; }
  .notice { padding: 7px 12px; border-radius: var(--r-md); font-size: var(--fs-sm); background: var(--bg-2); color: var(--text-1); border: 1px solid var(--line-0); }
  .notice.warn { background: var(--warn-bg); border-color: var(--warn); }
  .notice.error { background: var(--err-bg); border-color: var(--err); }
  .banner { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-radius: var(--r-lg); border: 1px solid var(--line-1); background: var(--bg-2); font-size: var(--fs-sm); }
  .banner.error, .banner.blocked { border-color: var(--warn); background: var(--warn-bg); }
  .summary { padding: 12px 14px; border: 1px solid var(--line-1); border-radius: var(--r-lg); background: var(--bg-1); display: flex; flex-direction: column; gap: 8px; }
  .sh { display: flex; align-items: center; gap: 10px; justify-content: space-between; flex-wrap: wrap; }
  .vf { font-size: var(--fs-xs); padding: 2px 9px; border-radius: var(--r-full); background: var(--bg-2); color: var(--text-2); }
  .vf.passed { background: var(--ok-bg); color: var(--ok); }
  .vf.failed { background: var(--err-bg); color: var(--err); }
  .vf.stale, .vf.none { background: var(--warn-bg); color: var(--text-1); }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .fbtn { display: flex; align-items: center; gap: 8px; width: 100%; padding: 4px 6px; border: 0; background: none; border-radius: var(--r-sm, 6px); cursor: pointer; color: var(--text-1); font: 12.5px var(--font-mono, ui-monospace, monospace); text-align: left; }
  .fbtn:hover { background: rgba(var(--fg-rgb), 0.05); }
  .fbtn :global(svg) { width: 13px; height: 13px; color: var(--accent); flex-shrink: 0; }
  .fbtn .truncate { flex: 1; min-width: 0; }
  .pm { color: var(--text-3); font-size: var(--fs-xs); white-space: nowrap; }
  .pm .a { color: var(--ok); font-style: normal; }
  .pm .r { color: var(--err); font-style: normal; }
  .cmds li { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--text-2); }
  .cmds li span:first-child { color: var(--ok); width: 12px; }
  .cmds li.bad span:first-child { color: var(--err); }
  .cmds code { flex: 1; min-width: 0; font-family: var(--font-mono, ui-monospace, monospace); }
  .warnbox { padding: 8px 12px; border-radius: var(--r-md); background: var(--err-bg); border: 1px solid var(--err); font-size: var(--fs-sm); }
  .sacts { display: flex; justify-content: flex-end; }
</style>
