<script lang="ts">
  import { Dices } from '@lucide/svelte';
  import type { ParamSpec } from '../api/types';
  import { byCategory, models as modelStore } from '../stores/models.svelte';
  import { apiFor } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import { toastError } from '../stores/toast.svelte';

  let { spec, value, onchange, disabled = false }: { spec: ParamSpec; value: unknown; onchange: (v: unknown) => void; disabled?: boolean } = $props();

  const models = $derived(spec.kind === 'model' && spec.model_category
    ? byCategory(spec.model_category, true, modelStore.nodesEngine).filter((m) => !spec.model_family || spec.model_family.includes(m.family ?? ''))
    : []);
  const enumOptions = $derived((spec.options ?? []).map((o) => (typeof o === 'string' ? { value: o, label: o.replace(/_/g, ' ') } : o)));

  function num(e: Event, int: boolean) {
    const raw = (e.currentTarget as HTMLInputElement).value;
    if (raw === '' || raw === '-') return;
    let v = int ? Math.round(Number(raw)) : Number(raw);
    if (Number.isNaN(v)) return;
    if (spec.min !== undefined && spec.min !== null) v = Math.max(spec.min, v);
    if (spec.max !== undefined && spec.max !== null) v = Math.min(spec.max, v);
    onchange(v);
  }

  async function upload(e: Event) {
    const f = (e.currentTarget as HTMLInputElement).files?.[0];
    if (!f) return;
    try {
      // Files go to the server whose node library the editor shows: the one that runs the graph.
      const r = await apiFor(modelStore.nodesEngine).upload(f, f.name);
      onchange(r.id);
    } catch (err) {
      toastError(err, 'Upload failed');
    }
  }
</script>

<label class="pw nodrag" class:disabled title={spec.description ?? ''}>
  <span class="pl">{spec.label}</span>
  {#if disabled}
    <span class="linked">← connected</span>
  {:else if spec.kind === 'int' || spec.kind === 'float'}
    <input class="num nowheel" type="number" value={value as number} step={spec.step ?? (spec.kind === 'int' ? 1 : 0.01)} min={spec.min} max={spec.max} onchange={(e) => num(e, spec.kind === 'int')} />
  {:else if spec.kind === 'seed'}
    <span class="seed">
      <input class="num nowheel" type="number" value={value as number} min={-1} onchange={(e) => num(e, true)} />
      <button type="button" class="dice" title="Random (−1 = new seed every run)" onclick={() => onchange(-1)}><Dices /></button>
    </span>
  {:else if spec.kind === 'bool'}
    <input type="checkbox" checked={!!value} onchange={(e) => onchange((e.currentTarget as HTMLInputElement).checked)} />
  {:else if spec.kind === 'enum'}
    <select class="nowheel" value={value as string} onchange={(e) => onchange((e.currentTarget as HTMLSelectElement).value)}>
      {#each enumOptions as o (o.value)}<option value={o.value}>{o.label}</option>{/each}
    </select>
  {:else if spec.kind === 'model'}
    <select class="nowheel" value={(value as string) ?? ''} onchange={(e) => onchange((e.currentTarget as HTMLSelectElement).value)}>
      {#if spec.optional || !value}<option value="">{spec.optional ? 'None' : 'Choose…'}</option>{/if}
      {#each models as m (m.id)}<option value={m.id}>{m.name}</option>{/each}
      {#if value && !models.some((m) => m.id === value)}<option value={value as string}>{value} (missing)</option>{/if}
    </select>
  {:else if spec.kind === 'file'}
    <span class="file">
      {#if value}<img src={mediaUrl(`/v1/files/${value}`, modelStore.nodesEngine)} alt="" />{/if}
      <input type="file" accept={spec.file_accept === 'video' ? 'video/*' : 'image/*'} onchange={upload} />
    </span>
  {:else if spec.kind === 'text'}
    <textarea class="nowheel" rows="3" value={(value as string) ?? ''} oninput={(e) => onchange((e.currentTarget as HTMLTextAreaElement).value)}></textarea>
  {:else}
    <input class="txt" type="text" value={(value as string) ?? ''} oninput={(e) => onchange((e.currentTarget as HTMLInputElement).value)} />
  {/if}
</label>

<style>
  .pw { display: grid; grid-template-columns: 92px 1fr; align-items: center; gap: 8px; font-size: 11.5px; min-height: 24px; }
  .pw:has(textarea), .pw:has(.file) { grid-template-columns: 1fr; gap: 4px; }
  .pl { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .disabled .pl { color: var(--lavender); }
  .linked { color: var(--text-3); font-style: italic; }
  input, select, textarea {
    width: 100%;
    min-width: 0;
    height: 24px;
    padding: 0 7px;
    border-radius: 6px;
    background: var(--bg-0);
    border: 1px solid var(--line-1);
    color: var(--text-0);
    font-size: 11.5px;
    outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: var(--select-line); }
  textarea { height: auto; padding: 6px 7px; resize: vertical; line-height: 1.45; font-family: inherit; user-select: text; }
  input[type='checkbox'] { width: 15px; height: 15px; justify-self: start; accent-color: var(--accent); }
  input[type='file'] { height: auto; padding: 3px; font-size: 10.5px; color: var(--text-2); }
  .seed { display: flex; gap: 4px; }
  .dice { display: grid; place-items: center; width: 24px; height: 24px; flex-shrink: 0; border: 1px solid var(--line-1); border-radius: 6px; background: var(--bg-3); color: var(--text-2); cursor: pointer; }
  .dice :global(svg) { width: 13px; height: 13px; }
  .file { display: flex; flex-direction: column; gap: 4px; }
  .file img { width: 100%; max-height: 120px; object-fit: contain; border-radius: 6px; background: #000; }
  .num { font-variant-numeric: tabular-nums; }
</style>
