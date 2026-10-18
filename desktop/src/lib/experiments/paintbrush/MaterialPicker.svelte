<script lang="ts">
  import { tooltip } from '../../ui/tooltip';
  import type { Material } from './types';

  /** Material swatches. Colours are only how a material looks here; the model receives its class, not the colour. */
  let { materials, value = $bindable(), onpick }: { materials: Material[]; value: number; onpick?: (m: Material) => void } = $props();

  const ORDER = ['Sky', 'Water', 'Vegetation', 'Terrain', 'Built'];
  const groups = $derived(
    [...new Set(materials.map((m) => m.group))]
      .sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99))
      .map((g) => ({ name: g, items: materials.filter((m) => m.group === g) })),
  );

  function tip(m: Material): string {
    const map = `Live model: COCO-Stuff “${m.coco_name}” · Refine: ${m.ade_index ? `ADE20K “${m.ade_name}”` : 'no ADE20K class'}`;
    const flag = m.live === 'experimental' ? 'Experimental. ' : m.refine === 'approximate' ? 'Approximate when refined. ' : '';
    return `${m.label} — ${flag}${m.note ? `${m.note} ` : ''}${map}`;
  }
</script>

<div class="materials" role="radiogroup" aria-label="Materials">
  {#each groups as g (g.name)}
    <div class="group">
      <div class="gname">{g.name}</div>
      <div class="grid">
        {#each g.items as m (m.id)}
          <button
            class="sw"
            class:on={value === m.index}
            role="radio"
            aria-checked={value === m.index}
            onclick={() => { value = m.index; onpick?.(m); }}
            use:tooltip={tip(m)}
          >
            <span class="chip" style:background={m.color}></span>
            <span class="label truncate">{m.label}</span>
            {#if m.live === 'experimental'}<span class="flag exp">beta</span>
            {:else if m.refine === 'approximate'}<span class="flag">≈</span>{/if}
          </button>
        {/each}
      </div>
    </div>
  {/each}
</div>

<style>
  .materials { display: flex; flex-direction: column; gap: 10px; }
  .gname { font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-3); padding: 0 2px 4px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 3px; }
  .sw {
    display: flex;
    align-items: center;
    gap: 7px;
    height: 30px;
    padding: 0 7px 0 5px;
    border: 1px solid transparent;
    border-radius: var(--r-sm);
    background: none;
    color: var(--text-1);
    font-size: var(--fs-sm);
    cursor: pointer;
    text-align: left;
    min-width: 0;
  }
  .sw:hover { background: rgba(var(--fg-rgb), 0.05); color: var(--text-0); }
  .sw.on { background: var(--select-bg); border-color: var(--select-line); color: var(--text-0); font-weight: 600; }
  .chip { width: 18px; height: 18px; border-radius: 5px; flex-shrink: 0; box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.14); }
  .label { flex: 1; min-width: 0; }
  .flag { font-size: 10px; color: var(--text-3); font-weight: 600; }
  .flag.exp { padding: 0 4px; border-radius: var(--r-full); background: var(--warn-bg); color: var(--text-2); }
</style>
