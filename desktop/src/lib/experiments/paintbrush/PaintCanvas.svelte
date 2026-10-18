<script lang="ts">
  import { Maximize, ZoomIn, ZoomOut } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import IconButton from '../../ui/IconButton.svelte';
  import { colorLut, paintRgba, type ColorLut, type Rect } from './engine';
  import type { PaintSession } from './session.svelte';

  /** The semantic painting: zoom with the wheel, pan with the hand tool, the middle button or Space + drag. */
  let { session, active = true }: { session: PaintSession; active?: boolean } = $props();

  let viewport: HTMLDivElement | undefined = $state();
  let canvasEl: HTMLCanvasElement | undefined = $state();
  let vw = $state(0);
  let vh = $state(0);
  let zoom = $state(1);
  let pan = $state({ x: 0, y: 0 });
  let cursor = $state<{ x: number; y: number } | null>(null);
  let spaceHeld = $state(false);
  let panning = $state(false);

  let image: ImageData | null = null;
  let lut: ColorLut = new Uint8ClampedArray(256 * 4);

  const fit = $derived(session.width && vw ? Math.min((vw - 24) / session.width, (vh - 24) / session.height) : 1);
  const scale = $derived(Math.max(0.05, fit * zoom));
  const left = $derived((vw - session.width * scale) / 2 + pan.x);
  const top = $derived((vh - session.height * scale) / 2 + pan.y);
  const handMode = $derived(session.tool === 'pan' || spaceHeld || panning);

  $effect(() => {
    lut = colorLut(Object.fromEntries(session.materials.map((m) => [m.index, m.color])));
    repaint(null);
  });

  function repaint(r: Rect | null) {
    const c = session.canvas;
    if (!c || !canvasEl) return;
    if (canvasEl.width !== c.w || canvasEl.height !== c.h || !image || image.width !== c.w || image.height !== c.h) {
      canvasEl.width = c.w;
      canvasEl.height = c.h;
      image = new ImageData(c.w, c.h);
      r = null;
    }
    const rect = r ?? c.full;
    paintRgba(c.comp, c.w, rect, lut, image.data);
    canvasEl.getContext('2d')!.putImageData(image, 0, 0, rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
  }

  onMount(() => {
    const off = session.onPaint(repaint);
    repaint(null);
    // non-passive, so the page never scrolls while zooming
    viewport!.addEventListener('wheel', wheel, { passive: false });
    return () => {
      off();
      viewport?.removeEventListener('wheel', wheel);
    };
  });

  function toCanvas(e: PointerEvent | WheelEvent): [number, number] {
    const r = canvasEl!.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * session.width, ((e.clientY - r.top) / r.height) * session.height];
  }

  let last: [number, number] | null = null;
  let panStart: { x: number; y: number; px: number; py: number } | null = null;

  function down(e: PointerEvent) {
    if (!canvasEl || !session.canvas) return;
    viewport!.setPointerCapture(e.pointerId);
    if (e.button === 1 || handMode) {
      panning = true;
      panStart = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    last = toCanvas(e);
    session.beginStroke(last[0], last[1]);
    if (session.tool !== 'brush' && session.tool !== 'eraser') last = null;
  }

  function move(e: PointerEvent) {
    const vr = viewport!.getBoundingClientRect();
    cursor = { x: e.clientX - vr.left, y: e.clientY - vr.top };
    if (panStart) {
      pan = { x: panStart.px + e.clientX - panStart.x, y: panStart.py + e.clientY - panStart.y };
      return;
    }
    if (!last) return;
    // Coalesced events keep fast strokes smooth (one dab segment per sampled pointer position).
    const evs = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : [];
    for (const ev of evs.length ? evs : [e]) {
      const p = toCanvas(ev);
      session.strokeTo(last[0], last[1], p[0], p[1]);
      last = p;
    }
  }

  function up(e: PointerEvent) {
    if (viewport?.hasPointerCapture(e.pointerId)) viewport.releasePointerCapture(e.pointerId);
    if (panStart) {
      panStart = null;
      panning = false;
      return;
    }
    if (last) session.endStroke();
    last = null;
  }

  function wheel(e: WheelEvent) {
    e.preventDefault();
    const vr = viewport!.getBoundingClientRect();
    const mx = e.clientX - vr.left;
    const my = e.clientY - vr.top;
    const next = Math.min(12, Math.max(0.25, zoom * Math.exp(-e.deltaY * 0.0015)));
    const k = next / zoom;
    // keep the canvas point under the cursor fixed
    const cx = mx - vw / 2 - pan.x;
    const cy = my - vh / 2 - pan.y;
    pan = { x: pan.x - cx * (k - 1), y: pan.y - cy * (k - 1) };
    zoom = next;
  }

  export function zoomBy(k: number) {
    zoom = Math.min(12, Math.max(0.25, zoom * k));
  }

  export function resetView() {
    zoom = 1;
    pan = { x: 0, y: 0 };
  }

  function key(e: KeyboardEvent) {
    if (!active || e.repeat) return;
    const t = e.target as HTMLElement;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.code === 'Space') {
      spaceHeld = e.type === 'keydown';
      if (spaceHeld) e.preventDefault();
    }
  }
</script>

<svelte:window onkeydown={key} onkeyup={key} onblur={() => (spaceHeld = false)} />

<div
  class="viewport"
  class:hand={handMode}
  class:grabbing={panning}
  class:crosshair={!handMode && (session.tool === 'fill' || session.tool === 'picker')}
  bind:this={viewport}
  bind:clientWidth={vw}
  bind:clientHeight={vh}
  onpointerdown={down}
  onpointermove={move}
  onpointerup={up}
  onpointercancel={up}
  onpointerleave={() => (cursor = null)}
  oncontextmenu={(e) => e.preventDefault()}
  role="application"
  aria-label="Painting canvas"
>
  <canvas
    bind:this={canvasEl}
    class:pixelated={scale > 1.6}
    style:left="{left}px"
    style:top="{top}px"
    style:width="{session.width * scale}px"
    style:height="{session.height * scale}px"
  ></canvas>
  {#if cursor && !handMode && (session.tool === 'brush' || session.tool === 'eraser')}
    <div
      class="brush"
      class:erase={session.tool === 'eraser'}
      style:left="{cursor.x}px"
      style:top="{cursor.y}px"
      style:width="{session.brush * scale}px"
      style:height="{session.brush * scale}px"
    ></div>
  {/if}
  <div class="zoom glass" onpointerdown={(e) => e.stopPropagation()} role="toolbar" aria-label="Zoom" tabindex="-1">
    <IconButton label="Zoom out" size="sm" onclick={() => zoomBy(1 / 1.25)}><ZoomOut /></IconButton>
    <button class="pct tnum" onclick={resetView} title="Fit to view">{Math.round(scale * 100)}%</button>
    <IconButton label="Zoom in" size="sm" onclick={() => zoomBy(1.25)}><ZoomIn /></IconButton>
    <IconButton label="Fit to view" size="sm" onclick={resetView}><Maximize /></IconButton>
  </div>
</div>

<style>
  .viewport {
    position: relative;
    flex: 1;
    min-height: 0;
    overflow: hidden;
    cursor: none;
    touch-action: none;
    background:
      linear-gradient(45deg, var(--bg-2) 25%, transparent 25%, transparent 75%, var(--bg-2) 75%) 0 0 / 16px 16px,
      linear-gradient(45deg, var(--bg-2) 25%, transparent 25%, transparent 75%, var(--bg-2) 75%) 8px 8px / 16px 16px,
      var(--bg-1);
  }
  .viewport.hand { cursor: grab; }
  .viewport.grabbing { cursor: grabbing; }
  .viewport.crosshair { cursor: crosshair; }
  canvas {
    position: absolute;
    border-radius: 2px;
    box-shadow: 0 0 0 1px var(--line-1), 0 6px 24px rgba(0, 0, 0, 0.12);
    image-rendering: auto;
  }
  canvas.pixelated { image-rendering: pixelated; }
  .brush {
    position: absolute;
    pointer-events: none;
    transform: translate(-50%, -50%);
    border-radius: 50%;
    border: 1.5px solid rgba(255, 255, 255, 0.95);
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.55), inset 0 0 0 1px rgba(0, 0, 0, 0.35);
    min-width: 4px;
    min-height: 4px;
  }
  .brush.erase { border-style: dashed; }
  .zoom {
    position: absolute;
    left: 10px;
    bottom: 10px;
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 3px;
    border-radius: var(--r-md);
    cursor: default;
  }
  .pct {
    min-width: 48px;
    height: 26px;
    border: 0;
    background: none;
    color: var(--text-1);
    font-size: var(--fs-xs);
    cursor: pointer;
    border-radius: var(--r-sm);
  }
  .pct:hover { background: rgba(var(--fg-rgb), 0.06); }
</style>
