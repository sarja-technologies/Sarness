<script lang="ts">
  import { Dices, ImageDown, Library, Sparkles, X } from '@lucide/svelte';
  import Button from '../../ui/Button.svelte';
  import Collapsible from '../../ui/Collapsible.svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import Segmented from '../../ui/Segmented.svelte';
  import Select from '../../ui/Select.svelte';
  import Slider from '../../ui/Slider.svelte';
  import TextArea from '../../ui/TextArea.svelte';
  import TextInput from '../../ui/TextInput.svelte';
  import Toggle from '../../ui/Toggle.svelte';
  import { tooltip } from '../../ui/tooltip';
  import { STYLES, type PaintSession } from './session.svelte';
  import type { MaskFormat, Quality } from './types';

  let { session }: { session: PaintSession } = $props();

  const s = $derived(session.settings);
  const st = $derived(session.status);
  const shown = $derived(session.show === 'refined' && session.refined ? session.refined : session.live);
  const outW = $derived(Math.round((session.width * s.scale) / 64) * 64);
  const outH = $derived(Math.round((session.height * s.scale) / 64) * 64);
  const scaleOk = (k: number) => !st || Math.round((session.width * k) / 64) * 64 * Math.round((session.height * k) / 64) * 64 <= st.refine.max_pixels;
  const style = $derived(STYLES.find((x) => x.guidance === s.guidance)?.id ?? null);
  let maskFormat = $state<MaskFormat>('color');
  let advanced = $state(false);

  function pickStyle(id: string) {
    const x = STYLES.find((y) => y.id === id)!;
    const set = session.settings;
    if (style === id) {
      set.guidance = '';
    } else {
      set.guidance = x.guidance;
      set.creativity = Math.max(set.creativity, x.creativity);
    }
    session.refine();
  }

  const fmt = (ms?: number | null) => (ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
</script>

<div class="gen">
  <div class="primary">
    <Button variant="primary" block loading={session.refining} disabled={!session.ready || !session.width}
            onclick={() => session.refine()}>
      <Sparkles />{session.canRefine ? 'Generate photo' : 'Render'}
    </Button>
    <div class="row">
      <Button size="sm" block disabled={!session.canRefine || !session.ready} onclick={() => session.refine({ variation: true })}>
        <Dices />New variation
      </Button>
      {#if session.refining}<IconButton label="Stop" size="sm" onclick={() => session.cancel()}><X /></IconButton>{/if}
    </div>
  </div>

  <div class="toggles">
    <Toggle bind:checked={session.settings.live} label="Live preview" hint="Re-render after every stroke" onchange={(v) => v && session.requestLive()} />
    <Toggle bind:checked={session.settings.autoRefine} label="Refine when I pause" hint="Photo pass after a short pause" disabled={!session.canRefine} />
  </div>

  <div class="sect">
    <span class="eyebrow">Style</span>
    <div class="chips">
      {#each STYLES as x (x.id)}
        <button class="chip" class:on={style === x.id} onclick={() => pickStyle(x.id)} disabled={!session.canRefine}>{x.label}</button>
      {/each}
    </div>
    <TextArea bind:value={session.settings.guidance} minRows={2} maxRows={5} placeholder="Describe the mood, season or light (optional)"
/>
    {#if s.guidance.trim() && s.creativity < 0.8}
      <p class="hint">Text guidance mostly sharpens detail below Creativity 0.8; raise it to change season or light.</p>
    {/if}
  </div>

  <div class="sect">
    <span class="eyebrow">Quality</span>
    <Segmented full size="sm" bind:value={session.settings.quality} items={[
      { value: 'draft' as Quality, label: 'Draft', title: `${st?.refine.quality.draft ?? 10} steps` },
      { value: 'standard' as Quality, label: 'Standard', title: `${st?.refine.quality.standard ?? 20} steps` },
      { value: 'high' as Quality, label: 'High', title: `${st?.refine.quality.high ?? 30} steps` },
    ]} />
    <Slider bind:value={session.settings.creativity} min={0.2} max={1} step={0.05} label="Creativity" format={(v) => v.toFixed(2)} />
    <div class="size">
      <span class="faint">Output</span>
      <Segmented size="sm" bind:value={session.settings.scale} items={(st?.refine.scales ?? [1, 1.5, 2]).map((k) => ({
        value: k, label: `${k}×`, disabled: !scaleOk(k),
        title: `${Math.round((session.width * k) / 64) * 64}×${Math.round((session.height * k) / 64) * 64}`,
      }))} />
      <span class="faint tnum">{outW}×{outH}</span>
    </div>
  </div>

  <Collapsible title="Advanced" bind:open={advanced}>
    <div class="sect">
      <Slider bind:value={session.settings.control} min={0.3} max={1.6} step={0.05} label="Follow the painting" format={(v) => v.toFixed(2)} />
      <div class="seed">
        <span class="faint">Seed</span>
        <TextInput size="sm" mono value={String(s.seed)} oninput={(e) => (session.settings.seed = Number((e.currentTarget as HTMLInputElement).value) || 0)} />
        <IconButton label="New seed" size="sm" onclick={() => session.refine({ variation: true })}><Dices /></IconButton>
      </div>
    </div>
  </Collapsible>

  <div class="sect out">
    <span class="eyebrow">Output</span>
    <div class="row">
      <Button size="sm" block disabled={!shown} onclick={() => session.saveToLibrary()}><Library />Save to Library</Button>
      <Button size="sm" block disabled={!shown} onclick={() => session.exportImage()}><ImageDown />Export</Button>
    </div>
    <div class="row">
      <Select size="sm" bind:value={maskFormat} options={[
        { value: 'color' as MaskFormat, label: 'Mask · as painted', hint: 'Interface colours' },
        { value: 'coco' as MaskFormat, label: 'Mask · COCO-Stuff ids', hint: 'What the live model reads (8-bit grey)' },
        { value: 'ade' as MaskFormat, label: 'Mask · ADE20K colours', hint: 'What the refine model reads' },
        { value: 'index' as MaskFormat, label: 'Mask · material indices', hint: '8-bit grey, 1…N' },
      ]} />
      <Button size="sm" disabled={!session.width} onclick={() => session.exportMask(maskFormat)}>Export</Button>
    </div>
  </div>

  {#if shown}
    <div class="meta faint tnum" use:tooltip={'Measured on the server for the image on screen'}>
      {#if shown.kind === 'refine'}
        Photo · seed {shown.seed} · {fmt(shown.timings.refine_ms)} on GPU · {fmt(shown.timings.total_ms)} total
      {:else}
        Live · {fmt(shown.timings.live_ms)} on GPU · {fmt(shown.timings.total_ms)} round trip
      {/if}
      {#if shown.peakVramMb}· peak {Math.round(shown.peakVramMb / 102.4) / 10} GB{/if}
    </div>
  {/if}
</div>

<style>
  .gen { display: flex; flex-direction: column; gap: 14px; }
  .primary { display: flex; flex-direction: column; gap: 6px; }
  .primary :global(svg) { width: 15px; height: 15px; }
  .row { display: flex; align-items: center; gap: 6px; }
  .row :global(svg) { width: 14px; height: 14px; }
  .toggles { display: flex; flex-direction: column; gap: 8px; }
  .sect { display: flex; flex-direction: column; gap: 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip {
    height: 26px;
    padding: 0 9px;
    border-radius: var(--r-full);
    border: 1px solid var(--line-1);
    background: var(--bg-0);
    color: var(--text-1);
    font-size: var(--fs-xs);
    cursor: pointer;
  }
  .chip:hover:not(:disabled) { border-color: var(--line-2); color: var(--text-0); }
  .chip.on { background: var(--select-bg); border-color: var(--select-line); color: var(--text-0); font-weight: 600; }
  .chip:disabled { opacity: 0.5; cursor: default; }
  .hint { font-size: var(--fs-xs); color: var(--text-2); }
  .size { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); }
  .seed { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); }
  .meta { font-size: var(--fs-xs); line-height: 1.5; }
</style>
