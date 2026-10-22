<script lang="ts">
  import { ImagePlus, X, Brush } from '@lucide/svelte';
  import { apiFor } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import { toastError } from '../stores/toast.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Spinner from '../ui/Spinner.svelte';

  interface Props {
    fileId: string | null;
    label?: string;
    hint?: string;
    accept?: string;
    onpaint?: () => void;
    painted?: boolean;
    /** Server to upload to (null = primary), and the map recording which server holds each file. */
    engine?: string | null;
    files?: Record<string, string>;
    /** Called with the new file (or null when removed), for callers that keep the id in a list. */
    onchange?: (id: string | null) => void;
  }
  let { fileId = $bindable(), label = 'Reference image', hint = 'Drop an image or click to browse', accept = 'image/*', onpaint, painted = false, engine = null, files, onchange }: Props = $props();

  function set(id: string | null) {
    fileId = id;
    onchange?.(id);
  }
  let uploading = $state(false);
  let over = $state(false);
  let input: HTMLInputElement;

  async function upload(f: File | undefined | null) {
    if (!f) return;
    uploading = true;
    try {
      const r = await apiFor(engine).upload(f, f.name);
      if (files && engine) files[r.id] = engine;
      set(r.id);
    } catch (e) {
      toastError(e, 'Upload failed');
    } finally {
      uploading = false;
    }
  }

  function drop(e: DragEvent) {
    e.preventDefault();
    over = false;
    const genId = e.dataTransfer?.getData('application/x-sarness-file');
    if (genId) {
      set(genId);
      return;
    }
    upload(e.dataTransfer?.files?.[0]);
  }
</script>

<div class="drop" class:over class:has={!!fileId} role="button" tabindex="0"
  ondragover={(e) => { e.preventDefault(); over = true; }} ondragleave={() => (over = false)} ondrop={drop}
  onclick={() => !fileId && input.click()} onkeydown={(e) => e.key === 'Enter' && !fileId && input.click()}>
  {#if uploading}
    <Spinner />
  {:else if fileId}
    <img src={mediaUrl(`/v1/files/${fileId}`, files?.[fileId] ?? engine)} alt="" />
    <div class="tools">
      {#if onpaint}<IconButton label={painted ? 'Edit mask' : 'Paint inpaint mask'} size="sm" active={painted} onclick={(e) => { e.stopPropagation(); onpaint(); }}><Brush /></IconButton>{/if}
      <IconButton label="Remove" size="sm" onclick={(e) => { e.stopPropagation(); set(null); }}><X /></IconButton>
    </div>
  {:else}
    <ImagePlus />
    <span class="l">{label}</span>
    <span class="h">{hint}</span>
  {/if}
  <input bind:this={input} type="file" {accept} hidden onchange={(e) => upload((e.currentTarget as HTMLInputElement).files?.[0])} />
</div>

<style>
  .drop {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-height: 92px;
    border-radius: var(--r-lg);
    border: 1px dashed var(--line-2);
    background: var(--bg-1);
    color: var(--text-3);
    cursor: pointer;
    overflow: hidden;
    transition: border-color var(--t-1), background var(--t-1);
  }
  .drop:hover, .over { border-color: var(--select-line); background: var(--select-bg); color: var(--text-1); }
  .has { cursor: default; border-style: solid; min-height: 120px; }
  .drop > :global(svg) { width: 20px; height: 20px; }
  .l { font-size: var(--fs-sm); color: var(--text-1); font-weight: 550; }
  .h { font-size: var(--fs-xs); }
  img { width: 100%; max-height: 180px; object-fit: contain; display: block; }
  .tools { position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; }
  .tools :global(button) { background: rgba(0, 0, 0, 0.6); backdrop-filter: blur(8px); }
</style>
