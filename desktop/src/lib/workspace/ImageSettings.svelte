<script lang="ts">
  import { Dices, Plus, Trash2 } from '@lucide/svelte';
  import type { InstalledModel } from '../api/types';
  import { byCategory } from '../stores/models.svelte';
  import ImageInput from '../studio/ImageInput.svelte';
  import MaskPainter from '../studio/MaskPainter.svelte';
  import { ASPECTS } from '../studio/aspect';
  import ModelSelect from '../components/ModelSelect.svelte';
  import Button from '../ui/Button.svelte';
  import Collapsible from '../ui/Collapsible.svelte';
  import Field from '../ui/Field.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Segmented from '../ui/Segmented.svelte';
  import Select from '../ui/Select.svelte';
  import Slider from '../ui/Slider.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { imageDims, type ImageSettings } from './params';
  import { randomSeed } from '../util/seed';

  /** Image-mode settings. Only options the selected model supports are shown. */
  let { s, model, setQuality }: { s: ImageSettings; model: InstalledModel | undefined; setQuality: (q: 'fast' | 'balanced' | 'best') => void } = $props();
  let maskOpen = $state(false);

  const caps = $derived(model?.capabilities ?? {});
  const limits = $derived(model?.limits ?? {});
  const dims = $derived(imageDims(s, model));
  const stepRange = $derived(limits.steps ?? [1, 100]);
  const cfgRange = $derived(limits.guidance ?? [0, 20]);
  const schedulerOptions = $derived([{ value: 'default', label: 'Model default' }, ...(caps.schedulers ?? []).map((x: string) => ({ value: x, label: x.replace(/_/g, ' ') }))]);
  const loraModels = $derived(byCategory('lora'));
</script>

<div class="ctl">
  <Field label="Aspect ratio">
    <div class="aspects">
      {#each ASPECTS as a (a.value)}
        <button class="asp" class:on={s.aspect === a.value} onclick={() => (s.aspect = a.value)} title={a.value}>
          <span class="shape" style="aspect-ratio:{a.w}/{a.h}"></span>
          <span>{a.value}</span>
        </button>
      {/each}
    </div>
  </Field>

  <div class="two">
    <Field label="Size" aside={undefined}>
      <Segmented bind:value={s.size} full items={[{ value: 'S', label: 'S' }, { value: 'M', label: 'M' }, { value: 'L', label: 'L' }]} />
    </Field>
    <Field label="Quality">
      <Segmented value={s.quality} full onchange={setQuality} items={[{ value: 'fast', label: 'Fast' }, { value: 'balanced', label: 'Std' }, { value: 'best', label: 'Best' }]} />
    </Field>
  </div>
  <div class="dims faint tnum">{dims[0]} × {dims[1]} px · {s.steps} steps{s.batch_size > 1 ? ` · ${s.batch_size} images` : ''}</div>

  {#if caps.modes?.includes('img2img') !== false}
    <Field label="Reference image" hint={s.reference_image ? (s.mask_image ? 'Inpainting: only the masked area is regenerated.' : 'Image-to-image: lower strength keeps more of the reference.') : 'Optional. Paint a mask on it to inpaint.'}>
      <ImageInput bind:fileId={s.reference_image} onpaint={caps.modes?.includes('inpaint') !== false ? () => (maskOpen = true) : undefined} painted={!!s.mask_image} />
    </Field>
    {#if s.reference_image && caps.denoise !== false}
      <Slider bind:value={s.denoise} min={0.05} max={1} step={0.01} label="Strength" format={(v) => v.toFixed(2)} />
    {/if}
  {/if}

  <Collapsible title="Advanced settings">
    {#if caps.negative_prompt !== false}
      <Field label="Negative prompt"><TextArea bind:value={s.negative_prompt} minRows={2} maxRows={6} placeholder="What to avoid…" /></Field>
    {/if}
    <Field label="Seed">
      <div class="row">
        <TextInput bind:value={s.seed} type="number" disabled={s.random_seed} mono size="sm" />
        <IconButton label="Random seed now" onclick={() => { s.seed = randomSeed(); s.random_seed = false; }}><Dices /></IconButton>
      </div>
    </Field>
    <Toggle bind:checked={s.random_seed} label="New random seed each run" />
    {#if caps.steps !== false}<Slider bind:value={s.steps} min={stepRange[0]} max={stepRange[1]} label="Steps" />{/if}
    {#if caps.guidance !== false}<Slider bind:value={s.guidance} min={cfgRange[0]} max={cfgRange[1]} step={0.1} label="Guidance (CFG)" format={(v) => v.toFixed(1)} />{/if}
    <Slider bind:value={s.batch_size} min={1} max={4} label="Batch size" />
    {#if caps.schedulers?.length}
      <Field label="Scheduler"><Select bind:value={s.scheduler} options={schedulerOptions} size="sm" /></Field>
    {/if}
    {#if caps.lora}
      <Field label="LoRAs" hint={loraModels.length ? '' : 'Install LoRAs from Models → Catalog or Hugging Face.'}>
        <div class="loras">
          {#each s.loras as l, i (i)}
            <div class="lora">
              <div class="lsel"><ModelSelect bind:value={l.id} category="lora" size="sm" /></div>
              <IconButton label="Remove LoRA" size="sm" onclick={() => s.loras.splice(i, 1)}><Trash2 /></IconButton>
              <div class="lw"><Slider bind:value={l.strength} min={-2} max={2} step={0.05} label="Strength" format={(v) => v.toFixed(2)} /></div>
            </div>
          {/each}
          <Button size="sm" variant="ghost" disabled={!loraModels.length} onclick={() => s.loras.push({ id: loraModels[0]?.id, strength: 1 })}><Plus />Add LoRA</Button>
        </div>
      </Field>
    {/if}
    {#if !caps.remote}
    <Field label="VAE"><ModelSelect bind:value={s.vae} category="vae" allowNone noneLabel="Model default" size="sm" /></Field>
    <Field label="Upscale result" hint="Runs a super-resolution model after generation.">
      <ModelSelect bind:value={s.upscale_model} category="upscaler" allowNone noneLabel="Off" size="sm" />
    </Field>
    {#if s.upscale_model}
      <Slider bind:value={s.upscale_factor} min={1} max={4} step={0.5} label="Upscale factor" format={(v) => `${v}×`} />
    {/if}
    <Field label="Memory mode" hint="Auto keeps the model on the GPU when it fits, otherwise offloads to system RAM.">
      <Select bind:value={s.memory_mode} size="sm" options={[{ value: 'auto', label: 'Auto' }, { value: 'gpu', label: 'GPU (fastest)' }, { value: 'offload', label: 'Offload (lower VRAM)' }, { value: 'sequential', label: 'Sequential (minimum VRAM)' }]} />
    </Field>
    <div class="two">
      <Field label="Format"><Select bind:value={s.format} size="sm" options={[{ value: 'png', label: 'PNG (with metadata)' }, { value: 'webp', label: 'WebP' }, { value: 'jpeg', label: 'JPEG' }]} /></Field>
      <Field label="CLIP skip"><Select bind:value={s.clip_skip} size="sm" options={[0, 1, 2].map((v) => ({ value: v, label: v ? String(v) : 'Off' }))} /></Field>
    </div>
    {:else}
      <p class="faint small">Runs on the server's AI gateway: memory mode, VAE, upscaling and output format are managed there. Output is PNG.</p>
    {/if}
  </Collapsible>

</div>

{#if maskOpen && s.reference_image}
  <MaskPainter bind:open={maskOpen} imageId={s.reference_image} bind:maskId={s.mask_image} />
{/if}

<style>
  .ctl { display: flex; flex-direction: column; gap: 16px; }
  .aspects { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
  .asp {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    padding: 8px 4px 6px;
    border-radius: var(--r-md);
    border: 1px solid var(--line-1);
    background: var(--bg-2);
    color: var(--text-2);
    font-size: var(--fs-xs);
    cursor: pointer;
    transition: border-color var(--t-1), background var(--t-1), color var(--t-1);
  }
  .asp:hover { border-color: var(--line-2); color: var(--text-0); }
  .asp.on { border-color: var(--select-line); background: var(--select-bg); color: var(--lavender); }
  .shape { width: auto; height: 20px; max-width: 34px; border-radius: 3px; border: 1.5px solid currentColor; opacity: 0.85; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .dims { font-size: var(--fs-xs); margin-top: -8px; }
  .loras { display: flex; flex-direction: column; gap: 10px; }
  .lora { display: grid; grid-template-columns: 1fr auto; gap: 6px; align-items: center; padding: 8px; border-radius: var(--r-md); background: var(--bg-2); border: 1px solid var(--line-0); }
  .lw { grid-column: 1 / -1; }
</style>
