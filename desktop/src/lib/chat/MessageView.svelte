<script lang="ts">
  import { Check, ChevronLeft, ChevronRight, Copy, FileText, PencilLine, RefreshCw, TriangleAlert } from '@lucide/svelte';
  import { mediaUrl } from '../api/transport';
  import type { Message } from '../api/types';
  import IconButton from '../ui/IconButton.svelte';
  import Button from '../ui/Button.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { handleCodeCopy, renderMarkdown } from './markdown';
  import Reasoning from './Reasoning.svelte';
  import WebSources from './WebSources.svelte';
  import { linkCitations } from './citations';
  import logo from '../assets/logo.svg';

  interface Props {
    m: Message;
    sibs: Message[];
    streaming: boolean;
    modelName?: string;
    /** Server storing the conversation (its attachments live there). */
    engine?: string | null;
    onswitch: (id: string) => void;
    onregenerate: () => void;
    onedit: (text: string) => void;
  }
  let { m, sibs, streaming, modelName = '', engine = null, onswitch, onregenerate, onedit }: Props = $props();
  let editing = $state(false);
  let draft = $state('');
  let copied = $state(false);

  const search = $derived(m.search ?? m.stats?.search);
  const html = $derived(m.role === 'assistant' ? linkCitations(renderMarkdown(m.content), search?.sources) : '');
  const idx = $derived(sibs.findIndex((s) => s.id === m.id));
  const stats = $derived(m.stats ?? {});

  function copy() {
    navigator.clipboard.writeText(m.content);
    copied = true;
    setTimeout(() => (copied = false), 1300);
  }
</script>

<div class="msg {m.role}" class:err={m.status === 'error'}>
  {#if m.role === 'assistant'}<img class="av" src={logo} alt="" width="26" height="26" />{/if}
  <div class="col">
    {#if m.attachments?.length}
      <div class="atts">
        {#each m.attachments as a (a.id)}
          {#if a.mime.startsWith('image/')}
            <img class="att" src={mediaUrl(a.url, engine)} alt={a.name ?? ''} />
          {:else}
            <span class="file"><FileText />{a.name ?? 'file'}</span>
          {/if}
        {/each}
      </div>
    {/if}

    {#if m.role === 'user'}
      {#if editing}
        <div class="editbox">
          <TextArea bind:value={draft} minRows={2} maxRows={16} />
          <div class="row" style="justify-content:flex-end">
            <Button size="sm" variant="ghost" onclick={() => (editing = false)}>Cancel</Button>
            <Button size="sm" variant="primary" onclick={() => { editing = false; onedit(draft); }}>Send edit</Button>
          </div>
        </div>
      {:else if m.content}
        <div class="bubble selectable">{m.content}</div>
      {/if}
    {:else}
      {#if search}<WebSources {search} running={streaming && !m.content && !m.reasoning} />{/if}
      <Reasoning reasoning={m.reasoning ?? ''} {streaming} answering={m.content.length > 0} />
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
      <div class="md selectable" onclick={handleCodeCopy}>
        {@html html}
        {#if streaming}<span class="caret"></span>{/if}
      </div>
      {#if search?.sources.length && !streaming}<WebSources {search} running={false} list />{/if}
      {#if m.status === 'error'}<p class="errtxt"><TriangleAlert />{stats.error ?? 'Generation failed'}</p>{/if}
      {#if m.status === 'stopped'}<p class="faint small">Stopped</p>{/if}
      {#if m.status === 'partial' && !streaming}<p class="errtxt"><TriangleAlert />Reply incomplete: the connection was interrupted. Use Regenerate to try again.</p>{/if}
    {/if}

    {#if !streaming && !editing}
      <div class="foot">
        {#if sibs.length > 1}
          <div class="branch">
            <IconButton label="Previous version" size="sm" disabled={idx <= 0} onclick={() => onswitch(sibs[idx - 1].id)}><ChevronLeft /></IconButton>
            <span class="tnum">{idx + 1} / {sibs.length}</span>
            <IconButton label="Next version" size="sm" disabled={idx >= sibs.length - 1} onclick={() => onswitch(sibs[idx + 1].id)}><ChevronRight /></IconButton>
          </div>
        {/if}
        <IconButton label={copied ? 'Copied' : 'Copy'} size="sm" onclick={copy}>{#if copied}<Check />{:else}<Copy />{/if}</IconButton>
        {#if m.role === 'user'}
          <IconButton label="Edit prompt (creates a branch)" size="sm" onclick={() => { draft = m.content; editing = true; }}><PencilLine /></IconButton>
        {:else}
          <IconButton label="Regenerate (creates a branch)" size="sm" onclick={onregenerate}><RefreshCw /></IconButton>
          {#if stats.tokens_per_s || stats.completion_tokens}
            <span class="stats tnum">
              {#if modelName}{modelName} · {/if}{#if stats.completion_tokens}{stats.completion_tokens} tokens{/if}{#if stats.tokens_per_s} · {stats.tokens_per_s} tok/s{/if}{#if stats.ttft_s} · first token {stats.ttft_s}s{/if}
            </span>
          {/if}
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .msg { display: flex; gap: 14px; padding: 6px 0; animation: rise-in var(--t-3) var(--ease-out); }
  .msg.user { justify-content: flex-end; }
  .av { flex-shrink: 0; margin-top: 2px; border-radius: 8px; }
  .col { display: flex; flex-direction: column; gap: 8px; min-width: 0; max-width: 100%; }
  .user .col { align-items: flex-end; max-width: 78%; }
  .assistant .col { flex: 1; }
  .bubble {
    padding: 10px 14px;
    border-radius: 16px 16px 4px 16px;
    background: linear-gradient(180deg, rgba(var(--accent-rgb), 0.2), rgba(var(--accent-rgb), 0.13));
    border: 1px solid rgba(var(--accent-rgb), 0.22);
    color: var(--text-0);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.6;
  }
  .editbox { width: min(640px, 70vw); display: flex; flex-direction: column; gap: 8px; }
  .atts { display: flex; flex-wrap: wrap; gap: 6px; }
  .att { max-width: 220px; max-height: 180px; border-radius: var(--r-md); border: 1px solid var(--line-1); }
  .file { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: var(--r-md); background: var(--bg-3); font-size: var(--fs-sm); }
  .file :global(svg) { width: 14px; height: 14px; }
  .md { font-size: 14.5px; line-height: 1.68; color: var(--text-0); overflow-wrap: anywhere; }
  .md :global(p) { margin: 0 0 0.75em; }
  .md :global(p:last-child) { margin-bottom: 0; }
  .md :global(h1), .md :global(h2), .md :global(h3) { margin: 1.1em 0 0.5em; font-size: 1.12em; }
  .md :global(ul), .md :global(ol) { margin: 0 0 0.8em; padding-left: 1.4em; }
  .md :global(li) { margin: 0.25em 0; }
  .md :global(a) { color: var(--accent-hi); }
  .md :global(a.cite) { display: inline-grid; place-items: center; min-width: 16px; height: 16px; padding: 0 3px; margin: 0 1px; border-radius: 8px; background: var(--bg-3); color: var(--text-1); font-size: 10.5px; font-weight: 600; text-decoration: none; vertical-align: 2px; }
  .md :global(a.cite:hover) { background: var(--accent); color: var(--text-inv); }
  .md :global(blockquote) { margin: 0 0 0.8em; padding: 2px 14px; border-left: 3px solid var(--accent); color: var(--text-1); }
  .md :global(code:not(.hljs)) { padding: 1px 6px; border-radius: 5px; background: rgba(var(--fg-rgb), 0.07); font-size: 0.88em; }
  .md :global(table) { border-collapse: collapse; margin: 0 0 0.9em; font-size: 0.95em; }
  .md :global(th), .md :global(td) { border: 1px solid var(--line-1); padding: 6px 10px; text-align: left; }
  .md :global(th) { background: var(--bg-3); }
  .md :global(hr) { border: 0; border-top: 1px solid var(--line-1); margin: 1.2em 0; }
  .md :global(.codeblock) { margin: 0.4em 0 1em; border-radius: var(--r-md); overflow: hidden; border: 1px solid var(--line-1); background: var(--bg-1); }
  .md :global(.cb-head) { display: flex; justify-content: space-between; align-items: center; padding: 5px 10px 5px 14px; background: var(--bg-3); font-size: 11.5px; color: var(--text-2); font-family: var(--font-mono); }
  .md :global(.cb-copy) { border: 0; background: rgba(var(--fg-rgb), 0.06); color: var(--text-1); border-radius: 6px; padding: 3px 9px; font-size: 11px; cursor: pointer; }
  .md :global(.cb-copy:hover) { background: rgba(var(--fg-rgb), 0.12); color: var(--text-0); }
  .md :global(pre) { margin: 0; padding: 12px 14px; overflow-x: auto; font-size: 12.8px; line-height: 1.6; }
  .md :global(.hljs-keyword), .md :global(.hljs-selector-tag), .md :global(.hljs-built_in) { color: var(--text-0); }
  .md :global(.hljs-string), .md :global(.hljs-attr) { color: var(--text-1); }
  .md :global(.hljs-number), .md :global(.hljs-literal) { color: var(--text-1); }
  .md :global(.hljs-comment) { color: var(--text-3); font-style: italic; }
  .md :global(.hljs-title), .md :global(.hljs-function) { color: var(--text-0); }
  .md :global(.hljs-type), .md :global(.hljs-class) { color: var(--text-1); }
  .md :global(.hljs-variable), .md :global(.hljs-params) { color: var(--text-1); }
  .md :global(.hljs-meta), .md :global(.hljs-tag) { color: var(--text-2); }
  .md :global(.hljs-addition) { color: var(--ok); }
  .md :global(.hljs-deletion) { color: var(--err); }
  .caret { display: inline-block; width: 8px; height: 1.05em; margin-left: 2px; vertical-align: text-bottom; border-radius: 2px; background: var(--lavender); animation: blink 1s steps(2) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .foot { display: flex; align-items: center; gap: 2px; opacity: 0; transition: opacity var(--t-2) var(--ease); min-height: 24px; }
  .msg:hover .foot, .foot:has(.branch) { opacity: 1; }
  .branch { display: flex; align-items: center; gap: 2px; font-size: var(--fs-xs); color: var(--text-2); margin-right: 4px; }
  .stats { margin-left: 8px; font-size: var(--fs-xs); color: var(--text-3); }
  .errtxt { display: flex; align-items: center; gap: 6px; color: var(--err); font-size: var(--fs-sm); }
  .errtxt :global(svg) { width: 14px; height: 14px; }
  .small { font-size: var(--fs-xs); }
</style>
