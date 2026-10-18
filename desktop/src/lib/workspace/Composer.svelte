<script lang="ts">
  import { ArrowUp, Clapperboard, Code2, Image as ImageIcon, MessagesSquare, Paperclip, SlidersHorizontal, Square, X } from '@lucide/svelte';
  import type { Snippet } from 'svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { MODE_LABEL, MODE_ORDER, stepMode, type Mode } from './params';

  export interface Attachment {
    id: string;
    name: string;
    url: string;
    image: boolean;
  }
  interface Props {
    mode: Mode;
    text: string;
    el?: HTMLTextAreaElement | null;
    placeholder: string;
    modelOptions: { value: string | null; label: string; group?: string; hint?: string }[];
    model: string | null;
    onmodel: (v: string | null) => void;
    chips: string[];
    attachments: Attachment[];
    accept: string;
    attachHint: string;
    uploading?: boolean;
    streaming: boolean;
    settingsOpen: boolean;
    settings: Snippet;
    onmode: (m: Mode) => void;
    onsend: () => void;
    onstop: () => void;
    onattach: (files: FileList | File[] | null | undefined) => void;
    onremove: (id: string) => void;
  }
  let {
    mode, text = $bindable(), el = $bindable(null), placeholder, modelOptions, model, onmodel, chips, attachments, accept, attachHint,
    uploading = false, streaming, settingsOpen = $bindable(), settings, onmode, onsend, onstop, onattach, onremove,
  }: Props = $props();

  const icons = { chat: MessagesSquare, code: Code2, image: ImageIcon, video: Clapperboard } as const;
  const tabs: Record<string, HTMLButtonElement> = {};

  function tabKeys(e: KeyboardEvent) {
    const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = stepMode(mode, d as 1 | -1);
    onmode(next);
    queueMicrotask(() => tabs[next]?.focus());
  }
  function paste(e: ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (files.length) {
      e.preventDefault();
      onattach(files);
    }
  }
  let panelEl: HTMLDivElement | undefined = $state();
  let fileInput: HTMLInputElement | undefined = $state();
  // Each mode's settings start at the top.
  $effect(() => {
    void mode;
    if (panelEl) panelEl.scrollTop = 0;
  });
  const canSend = $derived(!!text.trim() || attachments.length > 0);
</script>

<div class="wrap">
  {#if settingsOpen}
    <div class="panel" bind:this={panelEl} role="region" aria-label="{MODE_LABEL[mode]} settings">
      {@render settings()}
    </div>
  {/if}

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="composer" data-mode={mode} ondragover={(e) => e.preventDefault()} ondrop={(e) => { e.preventDefault(); onattach(e.dataTransfer?.files); }}>
    <div class="top">
      <div class="modes" role="radiogroup" aria-label="Mode" tabindex="-1" onkeydown={tabKeys}>
        {#each MODE_ORDER as m (m)}
          {@const Icon = icons[m]}
          <button bind:this={tabs[m]} type="button" role="radio" aria-checked={mode === m} tabindex={mode === m ? 0 : -1} class="mode" class:on={mode === m}
            title="{MODE_LABEL[m]}  ·  Ctrl+{MODE_ORDER.indexOf(m) + 1}" onclick={() => onmode(m)}>
            <Icon /><span class="lbl">{MODE_LABEL[m]}</span>
          </button>
        {/each}
      </div>
      <div class="spacer"></div>
      <div class="msel"><Select value={model} options={modelOptions} size="sm" onchange={onmodel} placeholder={mode === 'image' || mode === 'video' ? `No ${mode} model` : 'Choose a model'} /></div>
    </div>

    {#if attachments.length}
      <div class="pend">
        {#each attachments as a (a.id)}
          <span class="pchip">
            {#if a.image}<img src={a.url} alt="" />{/if}
            <span class="truncate">{a.name}</span>
            <button type="button" onclick={() => onremove(a.id)} aria-label="Remove {a.name}"><X /></button>
          </span>
        {/each}
      </div>
    {/if}

    <TextArea bind:el bind:value={text} minRows={1} maxRows={10} {placeholder}
      onkeydown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); onsend(); } }} onpaste={paste} />

    <div class="bottom">
      <button type="button" class="iconbtn" title={attachHint} aria-label="Attach: {attachHint}" onclick={() => fileInput?.click()}>
        {#if uploading}<Spinner size={14} />{:else}<Paperclip />{/if}
      </button>
      <input bind:this={fileInput} type="file" hidden multiple={mode === 'chat' || mode === 'code'} {accept} aria-hidden="true" tabindex="-1" onchange={(e) => { onattach((e.currentTarget as HTMLInputElement).files); (e.currentTarget as HTMLInputElement).value = ''; }} />
      <button type="button" class="settingsbtn" class:on={settingsOpen} aria-expanded={settingsOpen} onclick={() => (settingsOpen = !settingsOpen)}>
        <SlidersHorizontal /><span>{MODE_LABEL[mode]} settings</span>
      </button>
      <div class="chips" aria-live="polite">{#each chips as c (c)}<span class="chip">{c}</span>{/each}</div>
      <div class="spacer"></div>
      {#if streaming}
        <button type="button" class="send stop" onclick={onstop} aria-label="Stop generating"><Square /></button>
      {:else}
        <button type="button" class="send" onclick={onsend} disabled={!canSend} aria-label="Send"><ArrowUp /></button>
      {/if}
    </div>
  </div>
</div>

<style>
  .wrap { position: relative; width: 100%; max-width: 820px; margin: 0 auto; }
  .panel {
    position: absolute; left: 0; right: 0; bottom: calc(100% + 8px); max-height: min(60vh, 560px); overflow-y: auto;
    padding: 16px 18px 20px; border-radius: var(--r-xl); background: var(--bg-0); border: 1px solid var(--line-1);
    box-shadow: var(--shadow-3); animation: rise-in var(--t-2) var(--ease); z-index: 8;
  }
  .composer {
    container-type: inline-size;
    padding: 8px 10px 8px 10px; border-radius: 22px; background: var(--bg-1); border: 1px solid var(--line-1);
    box-shadow: var(--shadow-1); transition: border-color var(--t-1), box-shadow var(--t-2);
  }
  .composer:focus-within { border-color: var(--select-line); box-shadow: 0 0 0 3px rgba(var(--accent-rgb), 0.12); }
  .top { display: flex; align-items: center; gap: 8px; padding: 2px 2px 4px; }
  .modes { display: inline-flex; gap: 2px; padding: 2px; border-radius: var(--r-full); background: var(--bg-2); }
  .mode {
    display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 12px; border: 0; border-radius: var(--r-full);
    background: none; color: var(--text-2); font-size: var(--fs-sm); font-weight: 550; cursor: pointer;
    transition: background var(--t-1), color var(--t-1);
  }
  .mode :global(svg) { width: 14px; height: 14px; }
  .mode:hover { color: var(--text-0); }
  .mode.on { background: var(--bg-0); color: var(--text-0); box-shadow: var(--shadow-1); }
  .mode.on :global(svg) { color: var(--accent); }
  .msel { flex: 0 1 260px; min-width: 0; }
  .spacer { flex: 1; }
  .composer :global(textarea) { background: transparent; border: 0; box-shadow: none !important; font-size: 15px; padding: 8px 8px; resize: none; }
  .bottom { display: flex; align-items: center; gap: 6px; padding: 0 2px; min-height: 36px; }
  .iconbtn { display: grid; place-items: center; width: 32px; height: 32px; border: 0; background: none; border-radius: 10px; color: var(--text-2); cursor: pointer; }
  .iconbtn:hover { background: rgba(var(--fg-rgb), 0.06); color: var(--text-0); }
  .iconbtn :global(svg) { width: 17px; height: 17px; }
  .settingsbtn { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: var(--r-full); border: 1px solid transparent; background: none; color: var(--text-2); font-size: var(--fs-sm); cursor: pointer; white-space: nowrap; }
  .settingsbtn :global(svg) { width: 14px; height: 14px; }
  .settingsbtn:hover, .settingsbtn.on { background: var(--select-bg); color: var(--text-0); }
  .chips { display: flex; gap: 4px; min-width: 0; overflow: hidden; }
  .chip { padding: 2px 8px; border-radius: var(--r-full); background: var(--bg-2); color: var(--text-2); font-size: var(--fs-xs); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .send { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 12px; border: 0; background: var(--accent); color: var(--text-inv); cursor: pointer; transition: transform var(--t-1), opacity var(--t-1); flex-shrink: 0; }
  .send:hover:not(:disabled) { transform: translateY(-1px); }
  .send:disabled { opacity: 0.35; cursor: default; }
  .send :global(svg) { width: 17px; height: 17px; }
  .send.stop { background: var(--bg-5); color: var(--text-0); }
  .send.stop :global(svg) { width: 13px; height: 13px; fill: currentColor; }
  .pend { display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 2px 2px; }
  .pchip { display: inline-flex; align-items: center; gap: 6px; max-width: 220px; padding: 4px 6px 4px 4px; border-radius: var(--r-md); background: var(--bg-3); font-size: var(--fs-xs); }
  .pchip img { width: 28px; height: 28px; object-fit: cover; border-radius: 6px; }
  .pchip button { border: 0; background: none; color: var(--text-3); cursor: pointer; display: grid; }
  .pchip :global(svg) { width: 14px; height: 14px; }
  @container (max-width: 560px) { .lbl { display: none; } .mode { padding: 0 10px; } .settingsbtn span { display: none; } .chips { display: none; } }
</style>
