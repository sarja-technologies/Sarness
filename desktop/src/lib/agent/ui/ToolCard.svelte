<script lang="ts">
  import { Check, ChevronRight, FileMinus, FilePen, FilePlus, FileSearch, FolderOpen, GitBranch, ListChecks, MoveRight, Search, Terminal, X, CircleSlash, Stethoscope, Square } from '@lucide/svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import type { AgentItem } from '../types';
  import ApprovalCard from './ApprovalCard.svelte';
  import DiffView from './DiffView.svelte';

  type ToolItem = Extract<AgentItem, { kind: 'tool' }>;
  let { item, onapprove }: { item: ToolItem; onapprove: (approvalId: string, ok: boolean) => void } = $props();
  let open = $state(false);
  let term: HTMLPreElement | undefined = $state();

  const a = $derived(item.args as Record<string, any>);
  const ui = $derived(item.ui);
  const icon = $derived(
    ({ read_file: FileSearch, list_dir: FolderOpen, search: Search, find_files: Search, write_file: FilePlus, edit_file: FilePen, delete_path: FileMinus, move_path: MoveRight,
      run_command: Terminal, start_process: Terminal, process_output: Terminal, stop_process: Square, git_status: GitBranch, git_diff: GitBranch, git_log: GitBranch, git_branch: GitBranch, git_commit: GitBranch,
      diagnostics: Stethoscope, update_plan: ListChecks } as Record<string, any>)[item.name] ?? Terminal,
  );
  const busy = $derived(item.status === 'running');
  const title = $derived.by(() => {
    if (item.status === 'denied' && item.name === 'run_command') return `Not run: ${a.command ?? ''}`;
    if (item.status === 'denied' || item.status === 'cancelled') return `${item.status === 'denied' ? 'Not run' : 'Stopped'}: ${item.name.replace(/_/g, ' ')} ${a.path ?? a.command ?? ''}`;
    switch (item.name) {
      case 'read_file': return `Read ${a.path ?? ''}`;
      case 'list_dir': return `Listed ${a.path || 'project'}`;
      case 'search': return `Searched for ${JSON.stringify(a.pattern ?? '')}`;
      case 'find_files': return `Found files ${a.pattern ?? ''}`;
      case 'write_file': return `${ui?.created === false ? 'Rewrote' : 'Created'} ${a.path ?? ''}${ui ? ` +${ui.added ?? 0} −${ui.removed ?? 0}` : ''}`;
      case 'edit_file': return `Edited ${a.path ?? ''}${ui ? ` +${ui.added ?? 0} −${ui.removed ?? 0}` : ''}`;
      case 'delete_path': return `Deleted ${a.path ?? ''}`;
      case 'move_path': return `Moved ${a.from} → ${a.to}`;
      case 'run_command': return `${ui?.exit_code === 0 ? 'Ran' : busy || item.status === 'awaiting' ? 'Running' : 'Ran'} ${a.command ?? ''}`;
      case 'start_process': return `Started ${a.command ?? ''}`;
      case 'git_commit': return `Committed: ${a.message ?? ''}`;
      case 'update_plan': return 'Updated plan';
      default: return item.name.replace(/_/g, ' ') + (a.name ? ` ${a.name}` : '');
    }
  });
  const meta = $derived.by(() => {
    if (ui?.kind === 'command') {
      if (ui.cancelled) return 'stopped';
      if (ui.timed_out) return 'timed out';
      return `exit ${ui.exit_code ?? '?'} · ${(ui.duration_ms / 1000).toFixed(1)}s`;
    }
    return '';
  });
  const failed = $derived(item.status === 'failed');
  const terminal = $derived(ui?.kind === 'command' ? [ui.stdout, ui.stderr ? `\n[stderr]\n${ui.stderr}` : ''].join('') : (item.live ?? ''));
  // Running commands open automatically so output can be watched; scroll to follow it.
  $effect(() => {
    if (item.name === 'run_command' || item.name === 'start_process') {
      if (busy && item.live) open = true;
    }
  });
  // A finished command folds away when it succeeded; failures stay open so the cause is visible.
  let wasBusy = false;
  $effect(() => {
    if (busy) wasBusy = true;
    else if (wasBusy) {
      wasBusy = false;
      if (item.status === 'done') open = false;
    }
  });
  $effect(() => {
    void item.live;
    if (term && busy) term.scrollTop = term.scrollHeight;
  });
  const hasDetail = $derived(!!(item.live || ui?.kind === 'command' || ui?.diff || item.content));
</script>

<div class="tool" data-call={item.callId} class:failed class:denied={item.status === 'denied'} data-status={item.status}>
  <button type="button" class="row" aria-expanded={open} disabled={!hasDetail && !item.approval} onclick={() => (open = !open)}>
    <span class="st" aria-hidden="true">
      {#if busy}<Spinner size={13} />{:else if item.status === 'done'}<Check class="ok" />{:else if item.status === 'failed'}<X class="bad" />{:else if item.status === 'awaiting'}<span class="wait"></span>{:else}<CircleSlash class="dim" />{/if}
    </span>
    {#if icon}{@const I = icon}<I class="ti" />{/if}
    <span class="title truncate selectable">{title}</span>
    {#if meta}<span class="meta" class:bad={failed}>{meta}</span>{/if}
    {#if item.status === 'denied'}<span class="meta">{item.approval?.state === 'denied' ? 'declined by you' : 'not run'}</span>{:else if item.status === 'cancelled'}<span class="meta">stopped</span>{/if}
    {#if hasDetail}<ChevronRight class="chev {open ? 'open' : ''}" />{/if}
  </button>
  {#if item.approval}
    <ApprovalCard info={item.approval} onresolve={(ok) => onapprove(item.approval!.approvalId, ok)} />
  {/if}
  {#if open || (busy && item.live)}
    <div class="detail">
      {#if ui?.kind === 'command' || item.live}
        <div class="termhead"><span>{ui?.cwd ? `in ${ui.cwd || '.'}` : 'project folder'}</span>{#if ui?.truncated}<span>output shortened</span>{/if}</div>
        <pre class="term selectable" bind:this={term} aria-label="Terminal output">$ {a.command}
{terminal || (busy ? '' : '(no output)')}</pre>
      {:else if ui?.diff}
        <DiffView diff={ui.diff} />
      {:else if item.content}
        <pre class="term selectable">{item.content}</pre>
      {/if}
    </div>
  {/if}
</div>

<style>
  .tool { border: 1px solid var(--line-0); border-radius: var(--r-lg); background: var(--bg-1); overflow: hidden; }
  .tool.failed { border-color: rgba(var(--fg-rgb), 0.16); }
  .row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 34px; padding: 6px 10px; border: 0; background: none; color: var(--text-1); text-align: left; cursor: pointer; font-size: var(--fs-sm); }
  .row:disabled { cursor: default; }
  .row:not(:disabled):hover { background: rgba(var(--fg-rgb), 0.03); }
  .st { display: grid; place-items: center; width: 16px; flex-shrink: 0; }
  .st :global(svg) { width: 14px; height: 14px; }
  .st :global(.ok) { color: var(--ok); }
  .st :global(.bad) { color: var(--err); }
  .st :global(.dim) { color: var(--text-3); }
  .wait { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); }
  .row :global(.ti) { width: 14px; height: 14px; color: var(--accent); flex-shrink: 0; }
  .title { flex: 1; min-width: 0; font-family: var(--font-mono, ui-monospace, monospace); font-size: 12.5px; }
  .meta { color: var(--text-3); font-size: var(--fs-xs); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .meta.bad { color: var(--err); }
  .row :global(.chev) { width: 14px; height: 14px; color: var(--text-3); transition: transform var(--t-1); }
  .row :global(.chev.open) { transform: rotate(90deg); }
  .detail { padding: 0 10px 10px; }
  .termhead { display: flex; justify-content: space-between; color: var(--text-3); font-size: var(--fs-xs); padding: 0 2px 4px; }
  .term { margin: 0; padding: 10px 12px; max-height: 240px; overflow: auto; border-radius: var(--r-md); background: #15181a; color: #d7e0e0; font: 12px/1.5 var(--font-mono, ui-monospace, monospace); white-space: pre-wrap; word-break: break-word; }
</style>
