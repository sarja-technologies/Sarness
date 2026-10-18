<script lang="ts">
  import { Dices, Info } from '@lucide/svelte';
  import type { InstalledModel } from '../api/types';
  import ImageInput from '../studio/ImageInput.svelte';
  import Collapsible from '../ui/Collapsible.svelte';
  import Field from '../ui/Field.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Segmented from '../ui/Segmented.svelte';
  import Select from '../ui/Select.svelte';
  import Slider from '../ui/Slider.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextInput from '../ui/TextInput.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { videoDims, type VideoSettings } from './params';
  import { randomSeed } from '../util/seed';

  /** Video-mode settings. Only options the selected model supports are shown. */
  let { s, model, estimate = null }: { s: VideoSettings; model: InstalledModel | undefined; estimate?: string | null } = $props();

  const caps = $derived(model?.capabilities ?? {});
  const limits = $derived(model?.limits ?? {});
  const defaults = $derived((model?.defaults ?? {}) as Record<string, any>);
  const canI2V = $derived(!!caps.modes?.includes('i2v'));
  const resolutions = $derived((limits.resolutions ?? [[defaults.width ?? 832, defaults.height ?? 480]]).map(([w, h]: number[]) => ({ value: `${w}x${h}`, label: `${w} × ${h}`, hint: w > h ? 'Landscape' : w < h ? 'Portrait' : 'Square' })));
  const dims = $derived(videoDims(s, model));
  const frameRange = $derived(limits.frames ?? [5, 81]);
  const frameStep = $derived(limits.frame_step ?? 4);
  const fpsRange = $derived(limits.fps ?? [8, 30]);
  const seconds = $derived(s.frames / s.fps);

  $effect(() => {
    if (!canI2V && s.vmode === 'i2v') s.vmode = 't2v';
  });
</script>

<div class="ctl">
  {#if model?.notes}<p class="note"><Info />{model.notes}</p>{/if}
  <Segmented bind:value={s.vmode} full items={[
    { value: 't2v', label: 'Text → Video' },
    { value: 'i2v', label: 'Image → Video', disabled: !canI2V, title: canI2V ? undefined : 'This model does not support image-to-video' },
  ]} />

  {#if s.vmode === 'i2v'}
    <div class="frames" class:two={caps.end_frame}>
      <Field label="Start frame"><ImageInput bind:fileId={s.start_image} label="Start frame" /></Field>
      {#if caps.end_frame}<Field label="End frame"><ImageInput bind:fileId={s.end_image} label="End frame" /></Field>{/if}
    </div>
  {/if}

  <Field label="Resolution"><Select bind:value={s.resolution} options={resolutions} /></Field>
  <Slider bind:value={s.frames} min={frameRange[0]} max={frameRange[1]} step={frameStep} label="Length" format={(v) => `${v} frames · ${(v / s.fps).toFixed(1)} s`} />
  {#if fpsRange[0] !== fpsRange[1]}<Slider bind:value={s.fps} min={fpsRange[0]} max={fpsRange[1]} label="Frame rate" unit=" fps" />{/if}

  <div class="est">
    <span>{seconds.toFixed(1)} s clip · {dims[0]}×{dims[1]}</span>
    {#if estimate}<span class="faint">{estimate}</span>{/if}
  </div>

  <Collapsible title="Advanced settings">
    {#if caps.negative_prompt !== false}
      <Field label="Negative prompt" hint="Leave empty to use the model's recommended negative prompt.">
        <TextArea value={s.negative_prompt ?? ''} oninput={(e) => (s.negative_prompt = (e.currentTarget as HTMLTextAreaElement).value || null)} minRows={2} maxRows={6} />
      </Field>
    {/if}
    <Field label="Seed">
      <div class="row">
        <TextInput bind:value={s.seed} type="number" disabled={s.random_seed} mono size="sm" />
        <IconButton label="Random seed now" onclick={() => { s.seed = randomSeed(); s.random_seed = false; }}><Dices /></IconButton>
      </div>
    </Field>
    <Toggle bind:checked={s.random_seed} label="New random seed each run" />
    <Slider bind:value={s.steps} min={limits.steps?.[0] ?? 4} max={limits.steps?.[1] ?? 60} label="Steps" />
    {#if caps.guidance !== false}<Slider bind:value={s.guidance} min={limits.guidance?.[0] ?? 1} max={limits.guidance?.[1] ?? 10} step={0.1} label="Guidance" format={(v) => v.toFixed(1)} />{/if}
    {#if caps.flow_shift !== false}<Slider bind:value={s.flow_shift} min={0.5} max={12} step={0.1} label="Flow shift" format={(v) => v.toFixed(1)} />{/if}
    {#if caps.remote}
      <p class="faint small">Runs on the server's AI gateway: memory placement is managed there. Output is MP4 at 24 fps.</p>
    {:else}
    <Field label="Memory mode" hint="Split keeps the video transformer on the GPU and streams the large text encoder from system RAM.">
      <Select bind:value={s.memory_mode} size="sm" options={[{ value: 'auto', label: 'Auto' }, { value: 'gpu', label: 'GPU (needs ~16 GB+)' }, { value: 'split', label: 'Split' }, { value: 'offload', label: 'Offload' }, { value: 'sequential', label: 'Sequential (slowest)' }]} />
    </Field>
    <Field label="Format"><Select bind:value={s.format} size="sm" options={[{ value: 'mp4', label: 'MP4 (H.264)' }, { value: 'webm', label: 'WebM (VP9)' }, { value: 'gif', label: 'GIF' }]} /></Field>
    {/if}
    {#if !caps.motion?.length}<p class="faint small">This model has no dedicated motion controls; describe camera and subject motion in the prompt.</p>{/if}
  </Collapsible>

</div>

<style>
  .ctl { display: flex; flex-direction: column; gap: 16px; }
  .frames { display: grid; gap: 10px; }
  .frames.two { grid-template-columns: 1fr 1fr; }
  .note { display: flex; gap: 8px; font-size: var(--fs-xs); color: var(--text-2); line-height: 1.5; padding: 8px 10px; border-radius: var(--r-md); background: var(--info-bg); margin-top: -6px; }
  .note :global(svg) { width: 14px; height: 14px; color: var(--info); flex-shrink: 0; margin-top: 1px; }
  .est { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-xs); color: var(--text-1); padding: 8px 10px; border-radius: var(--r-md); background: var(--bg-2); flex-wrap: wrap; }
  .small { font-size: var(--fs-xs); }
</style>
