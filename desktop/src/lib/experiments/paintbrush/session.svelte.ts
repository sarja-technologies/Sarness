// God's Paintbrush session: the painting, its live / refined results and the request loop to the server.
//
// Live previews are latest-wins on both ends: at most one request is in flight from here (newer edits wait and
// are sent as one), and the engine drops stale requests per session. A refine is pre-empted by the next edit and
// re-run after a short pause when "Refine when I pause" is on.

import { invoke } from '@tauri-apps/api/core';
import { save } from '@tauri-apps/plugin-dialog';
import { revealItemInDir } from '@tauri-apps/plugin-opener';
import { ApiError } from '../../api/client';
import { exps, fromExperimentsEngine, xapi } from '../store.svelte';
import { isTauri, mediaUrl } from '../../api/transport';
import { onEvent } from '../../stores/connection.svelte';
import { persistPrefs, prefs } from '../../stores/prefs.svelte';
import { toast, toastError } from '../../stores/toast.svelte';
import { randomSeed, safeSeed } from '../../util/seed';
import type { ExperimentDocument } from '../types';
import { deserialize, serialize, type PaintDocData, type PaintSettings } from './doc';
import { LabelCanvas, colorLut, encodeMask, paintRgba, type Rect } from './engine';
import type { MaskExport, MaskFormat, Material, PaintbrushStatus, RenderResult } from './types';

export const PB = '/v1/experiments/gods-paintbrush';
const DRAFT_KEY = 'sarness.paintbrush.draft.v1';
const REFINE_PAUSE_MS = 900;
const BUSY_RETRY_MS = 4000;

export type Tool = 'brush' | 'eraser' | 'fill' | 'picker' | 'pan';

export interface Shown {
  url: string;
  renderId: string;
  kind: 'live' | 'refine';
  width: number;
  height: number;
  /** Painting version this image was rendered from. */
  version: number;
  timings: RenderResult['timings'];
  peakVramMb: number | null;
  seed?: number;
  prompt?: string;
  at: number;
}

export const DEFAULT_SETTINGS: PaintSettings = {
  live: true,
  autoRefine: true,
  quality: 'standard',
  creativity: 0.7,
  control: 1.0,
  scale: 1,
  seed: 0,
  guidance: '',
};

/** One-click looks, like Canvas styles: they set text guidance and enough creativity for it to show. */
export const STYLES: { id: string; label: string; guidance: string; creativity: number }[] = [
  { id: 'golden', label: 'Golden hour', guidance: 'golden hour, warm low sunlight, long shadows', creativity: 0.85 },
  { id: 'overcast', label: 'Overcast', guidance: 'overcast sky, soft diffuse light, moody', creativity: 0.85 },
  { id: 'winter', label: 'Winter', guidance: 'winter, fresh snow, cold clear air', creativity: 0.9 },
  { id: 'autumn', label: 'Autumn', guidance: 'autumn, orange and red foliage, golden grass', creativity: 0.85 },
  { id: 'misty', label: 'Misty morning', guidance: 'misty morning, fog in the valleys, soft light', creativity: 0.85 },
  { id: 'sunset', label: 'Sunset', guidance: 'dramatic sunset, glowing clouds, vivid sky', creativity: 0.9 },
  { id: 'night', label: 'Moonlit', guidance: 'night, moonlight, stars, blue hour', creativity: 0.95 },
];

function busyOrCancelled(e: unknown): 'superseded' | 'busy' | null {
  if (!(e instanceof ApiError)) return null;
  if (e.status === 409 && /superseded|cancelled/.test(e.message)) return 'superseded';
  if (e.status === 503) return 'busy';
  return null;
}

async function loadImage(src: string): Promise<HTMLImageElement> {
  const img = new Image();
  img.decoding = 'async';
  img.src = src;
  await img.decode();
  return img;
}

/** Saves an engine file through the native dialog (desktop) or opens it (browser). */
export async function saveEngineFile(fileId: string, url: string, defaultName: string, filter: { name: string; ext: string }) {
  if (!isTauri) {
    window.open(mediaUrl(url, exps.engine), '_blank');
    return;
  }
  const dest = await save({ defaultPath: defaultName, filters: [{ name: filter.name, extensions: [filter.ext] }] });
  if (!dest) return;
  await invoke('export_file', { fileId, dest, engine: exps.engine });
  toast('success', 'Exported', dest, { action: { label: 'Show', run: () => revealItemInDir(dest) } });
}

export class PaintSession {
  readonly session = `pb-${Math.random().toString(36).slice(2, 10)}`;

  status = $state<PaintbrushStatus | null>(null);
  statusError = $state<string | null>(null);
  materials = $derived<Material[]>(this.status?.materials ?? []);
  ready = $derived(this.status?.availability.state === 'ready' || this.status?.availability.state === 'partial');
  canRefine = $derived(!!this.status?.availability.refine);

  // ---- painting (the label canvas itself is not reactive; `version` is)
  canvas: LabelCanvas | null = null;
  version = $state(0);
  width = $state(0);
  height = $state(0);
  layers = $state<{ id: string; name: string; visible: boolean }[]>([]);
  activeLayer = $state('');
  canUndo = $state(false);
  canRedo = $state(false);
  tool = $state<Tool>('brush');
  brush = $state(28);
  material = $state(0);

  settings = $state<PaintSettings>({ ...DEFAULT_SETTINGS, seed: randomSeed() });
  doc = $state({ id: null as string | null, title: 'Untitled painting', dirty: false, savedAt: 0 });

  // ---- results
  live = $state<Shown | null>(null);
  refined = $state<Shown | null>(null);
  show = $state<'live' | 'refined'>('live');
  rendering = $state(false);
  refining = $state(false);
  progress = $state<{ step: number; total: number } | null>(null);
  error = $state<string | null>(null);
  gpuBusy = $state<string | null>(null);
  saving = $state(false);

  private listeners = new Set<(r: Rect | null) => void>();
  private inFlight = false;
  private pending = false;
  private refineTimer: ReturnType<typeof setTimeout> | null = null;
  private busyTimer: ReturnType<typeof setTimeout> | null = null;
  private draftTimer: ReturnType<typeof setTimeout> | null = null;
  private refineVersion = -1;
  private unsubs: (() => void)[] = [];

  constructor() {
    this.unsubs.push(
      onEvent('experiment.progress', (e) => {
        if (!fromExperimentsEngine(e)) return;
        if (e.data?.session === this.session && this.refining) this.progress = { step: e.data.step, total: e.data.total };
      }),
      onEvent('experiment.status', (e) => {
        if (!fromExperimentsEngine(e)) return;
        if (e.data?.experiment === 'gods-paintbrush' && this.status) this.status.runtime = e.data.runtime;
      }),
      onEvent('experiment.install', (e) => {
        if (!fromExperimentsEngine(e)) return;
        if (e.data?.experiment !== 'gods-paintbrush' || !this.status) return;
        const { experiment: _, ...state } = e.data;
        this.status.install = state;
        if (!state.running) this.loadStatus();
      }),
    );
  }

  destroy() {
    this.unsubs.forEach((u) => u());
    for (const t of [this.refineTimer, this.busyTimer, this.draftTimer]) if (t) clearTimeout(t);
    void this.writeDraft();
  }

  // ------------------------------------------------------------- status
  async loadStatus(): Promise<void> {
    try {
      this.status = await xapi().get<PaintbrushStatus>(`${PB}/status`);
      this.statusError = null;
    } catch (e) {
      this.statusError = e instanceof ApiError && e.status === 404
        ? 'This Sarness Engine does not have God\'s Paintbrush yet — update the engine on the server.'
        : e instanceof Error ? e.message : String(e);
    }
  }

  /** First open: status, then the saved draft (or a starter scene), then a first live frame. */
  async init(): Promise<void> {
    await this.loadStatus();
    if (!this.status) return;
    if (!this.canvas) {
      const restored = await this.restoreDraft();
      if (!restored) this.newPainting(this.status.canvas.sizes[0]?.width ?? 768, this.status.canvas.sizes[0]?.height ?? 512);
    }
    if (!this.material) this.material = this.idx('mountains') || this.materials[0]?.index || 1;
    this.requestLive();
  }

  idx(id: string): number {
    return this.materials.find((m) => m.id === id)?.index ?? 0;
  }

  materialAt(index: number): Material | undefined {
    return this.materials.find((m) => m.index === index);
  }

  // ------------------------------------------------------------ painting
  onPaint(fn: (r: Rect | null) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private attach(c: LabelCanvas) {
    this.canvas = c;
    this.width = c.w;
    this.height = c.h;
    this.activeLayer = c.layers[c.layers.length - 1].id;
    this.live = null;
    this.refined = null;
    this.show = 'live';
    this.refineVersion = -1;
    this.changed(null, false);
  }

  /** A new painting: sky over a strip of grass, so the first frame is already a landscape. */
  newPainting(width: number, height: number): void {
    const sky = this.idx('sky') || 1;
    const c = new LabelCanvas(width, height, sky);
    const grass = this.idx('grass');
    if (grass) c.layers[0].data.fill(grass, Math.floor(height * 0.62) * width);
    c.layers[0].name = 'Base';
    c.recomposite();
    this.attach(c);
    this.doc = { id: null, title: 'Untitled painting', dirty: false, savedAt: 0 };
    this.settings = { ...this.settings, seed: randomSeed() };
    this.writeDraft();
  }

  /** Called after every change to pixels or layers. */
  changed(rect: Rect | null, dirty = true): void {
    const c = this.canvas;
    if (!c) return;
    this.version++;
    this.layers = c.layers.map((l) => ({ id: l.id, name: l.name, visible: l.visible }));
    if (!c.layer(this.activeLayer)) this.activeLayer = c.layers[c.layers.length - 1].id;
    this.canUndo = c.canUndo;
    this.canRedo = c.canRedo;
    for (const fn of this.listeners) fn(rect);
    if (dirty) this.doc.dirty = true;
    this.scheduleDraft();
    this.requestLive();
  }

  beginStroke(x: number, y: number): void {
    const c = this.canvas;
    if (!c) return;
    if (this.tool === 'picker') {
      const v = c.pick(x, y);
      if (v) this.material = v;
      return;
    }
    if (this.tool === 'fill') {
      const r = c.fill(this.activeLayer, x, y, this.material);
      if (r) this.changed(r);
      return;
    }
    if (this.tool !== 'brush' && this.tool !== 'eraser') return;
    if (!c.beginStroke(this.activeLayer)) return;
    const r = c.dab(x, y, this.brush / 2, this.tool === 'eraser' ? 0 : this.material);
    if (r) this.changed(r);
  }

  strokeTo(x0: number, y0: number, x1: number, y1: number): void {
    const c = this.canvas;
    if (!c || (this.tool !== 'brush' && this.tool !== 'eraser')) return;
    const r = c.line(x0, y0, x1, y1, this.brush / 2, this.tool === 'eraser' ? 0 : this.material);
    if (r) this.changed(r);
  }

  endStroke(): void {
    const c = this.canvas;
    if (!c) return;
    c.endStroke();
    this.canUndo = c.canUndo;
    this.canRedo = c.canRedo;
  }

  undo() {
    if (this.canvas?.undo()) this.changed(null);
  }

  redo() {
    if (this.canvas?.redo()) this.changed(null);
  }

  clear(all = true) {
    if (!this.canvas) return;
    if (all) this.canvas.clear(undefined, this.idx('sky') || this.canvas.background);
    else this.canvas.clear(this.activeLayer);
    this.changed(null);
  }

  addLayer() {
    if (!this.canvas) return;
    const l = this.canvas.addLayer(`Layer ${this.canvas.layers.length + 1}`, this.activeLayer);
    this.activeLayer = l.id;
    this.changed(null);
  }

  removeLayer(id: string) {
    this.canvas?.removeLayer(id);
    this.changed(null);
  }

  moveLayer(id: string, delta: number) {
    this.canvas?.moveLayer(id, delta);
    this.changed(null);
  }

  toggleLayer(id: string) {
    const l = this.canvas?.layer(id);
    if (!l) return;
    this.canvas!.setVisible(id, !l.visible);
    this.changed(null);
  }

  renameLayer(id: string, name: string) {
    this.canvas?.rename(id, name);
    this.changed(null, true);
  }

  setBackground(index: number) {
    if (!this.canvas) return;
    this.canvas.structural((c) => {
      c.background = index;
    });
    this.changed(null);
  }

  // ------------------------------------------------------------- rendering
  requestLive(): void {
    if (!this.canvas || !this.ready || !this.settings.live) return;
    if (this.inFlight) {
      this.pending = true;
      return;
    }
    void this.runLive();
  }

  private async runLive(): Promise<void> {
    const c = this.canvas;
    if (!c) return;
    this.inFlight = true;
    this.pending = false;
    this.rendering = true;
    const version = this.version;
    try {
      const mask = await encodeMask(c.comp.slice(), c.w, c.h);
      const r = await xapi().post<RenderResult>(`${PB}/render`, { session: this.session, kind: 'live', mask });
      if (c === this.canvas && version >= (this.live?.version ?? -1)) {
        this.live = this.shown(r, version);
        if (!this.refined || this.refined.version !== this.version) this.show = 'live';
      }
      this.error = null;
      this.gpuBusy = null;
    } catch (e) {
      this.handleError(e, () => this.requestLive());
    } finally {
      this.inFlight = false;
      this.rendering = false;
      if (this.pending) this.requestLive();
      else this.scheduleRefine();
    }
  }

  private shown(r: RenderResult, version: number): Shown {
    return { url: r.image, renderId: r.render_id, kind: r.kind, width: r.width, height: r.height, version,
             timings: r.timings, peakVramMb: r.peak_vram_mb, seed: r.seed, prompt: r.prompt, at: Date.now() };
  }

  private handleError(e: unknown, retry: () => void) {
    const kind = busyOrCancelled(e);
    if (kind === 'superseded') return;
    if (kind === 'busy') {
      this.gpuBusy = (e as Error).message;
      if (this.busyTimer) clearTimeout(this.busyTimer);
      this.busyTimer = setTimeout(() => {
        this.busyTimer = null;
        if (this.gpuBusy) retry();
      }, BUSY_RETRY_MS);
      return;
    }
    this.error = e instanceof Error ? e.message : String(e);
  }

  private scheduleRefine() {
    if (this.refineTimer) clearTimeout(this.refineTimer);
    if (!this.settings.autoRefine || !this.canRefine || this.gpuBusy) return;
    const v = this.version;
    this.refineTimer = setTimeout(() => {
      this.refineTimer = null;
      if (v === this.version && this.refineVersion !== v && !this.refining && !this.inFlight) void this.refine();
    }, REFINE_PAUSE_MS);
  }

  /** Renders a refined photo of the current painting with the current seed (or a new one for a variation). */
  async refine(opts: { variation?: boolean } = {}): Promise<void> {
    const c = this.canvas;
    if (!c || !this.ready) return;
    if (!this.canRefine) {
      this.requestLive();
      return;
    }
    if (opts.variation) this.settings.seed = randomSeed();
    const version = this.version;
    this.refineVersion = version;
    this.refining = true;
    this.progress = null;
    const s = this.settings;
    try {
      const mask = await encodeMask(c.comp.slice(), c.w, c.h);
      const r = await xapi().post<RenderResult>(`${PB}/render`, {
        session: this.session, kind: 'refine', mask, seed: safeSeed(s.seed) < 0 ? randomSeed() : safeSeed(s.seed),
        quality: s.quality, creativity: s.creativity, control: s.control, scale: s.scale, prompt: s.guidance,
      });
      if (c === this.canvas && version === this.version) {
        this.refined = this.shown(r, version);
        this.show = 'refined';
      }
      this.error = null;
      this.gpuBusy = null;
    } catch (e) {
      this.refineVersion = -1;
      this.handleError(e, () => this.refine());
    } finally {
      this.refining = false;
      this.progress = null;
    }
  }

  async cancel(): Promise<void> {
    try {
      await xapi().post(`${PB}/cancel`, { session: this.session });
    } catch {
      /* nothing running */
    }
  }

  /** The image on screen (refined when it matches the painting, else the live frame). */
  current(): Shown | null {
    return this.show === 'refined' && this.refined ? this.refined : this.live;
  }

  // ------------------------------------------------------------- outputs
  async saveToLibrary(): Promise<void> {
    const r = this.current();
    if (!r) return;
    try {
      await xapi().post(`${PB}/renders/${r.renderId}/save`, {
        project_id: prefs.projectId, folder_id: prefs.folderId, document_id: this.doc.id,
      });
      toast('success', 'Saved to Library', r.kind === 'refine' ? 'Refined photo with its painting and settings.' : 'Live frame with its painting.', {
        action: { label: 'Open Library', run: () => { prefs.view = 'library'; persistPrefs(); } },
      });
    } catch (e) {
      toastError(e, 'Could not save to the Library');
    }
  }

  async exportImage(): Promise<void> {
    const r = this.current();
    if (!r) return;
    try {
      const f = await xapi().post<{ file_id: string; url: string; mime: string; name: string }>(`${PB}/renders/${r.renderId}/export`);
      const ext = f.name.split('.').pop() || 'png';
      await saveEngineFile(f.file_id, f.url, `${this.fileStem()}.${ext}`, { name: 'Image', ext });
    } catch (e) {
      toastError(e, 'Export failed');
    }
  }

  async exportMask(format: MaskFormat): Promise<void> {
    const c = this.canvas;
    if (!c) return;
    try {
      const mask = await encodeMask(c.comp.slice(), c.w, c.h);
      const f = await xapi().post<MaskExport>(`${PB}/mask/export`, { mask, format });
      const suffix = { color: 'colours', coco: 'coco-stuff', ade: 'ade20k', index: 'materials' }[format];
      await saveEngineFile(f.file_id, f.url, `${this.fileStem()}-mask-${suffix}.png`, { name: 'PNG image', ext: 'png' });
    } catch (e) {
      toastError(e, 'Mask export failed');
    }
  }

  private fileStem(): string {
    return (this.doc.title || 'painting').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48) || 'painting';
  }

  // ----------------------------------------------------------- documents
  /** Small JPEG of the latest result (or of the painting itself before anything was rendered). */
  private async thumbnail(): Promise<string | null> {
    const c = this.canvas;
    if (!c) return null;
    try {
      const out = document.createElement('canvas');
      out.width = 320;
      out.height = Math.round((320 * c.h) / c.w);
      const ctx = out.getContext('2d')!;
      const src = this.current()?.url;
      if (src) {
        ctx.drawImage(await loadImage(src), 0, 0, out.width, out.height);
      } else {
        const full = document.createElement('canvas');
        full.width = c.w;
        full.height = c.h;
        const img = new ImageData(c.w, c.h);
        paintRgba(c.comp, c.w, c.full, colorLut(Object.fromEntries(this.materials.map((m) => [m.index, m.color]))), img.data);
        full.getContext('2d')!.putImageData(img, 0, 0);
        ctx.drawImage(full, 0, 0, out.width, out.height);
      }
      return out.toDataURL('image/jpeg', 0.82);
    } catch {
      return null;
    }
  }

  private async data(): Promise<PaintDocData> {
    return serialize(this.canvas!, this.materials, $state.snapshot(this.settings) as PaintSettings,
                     { live: 'spade-landscapes-512', refine: 'sd15-controlnet-seg' });
  }

  async save(opts: { asCopy?: boolean; title?: string } = {}): Promise<boolean> {
    if (!this.canvas) return false;
    this.saving = true;
    try {
      const body = { title: opts.title ?? (opts.asCopy ? `${this.doc.title} (copy)` : this.doc.title),
                     data: await this.data(), thumbnail: await this.thumbnail(), project_id: prefs.projectId };
      const d = this.doc.id && !opts.asCopy
        ? await xapi().put<ExperimentDocument>(`${PB}/documents/${this.doc.id}`, body)
        : await xapi().post<ExperimentDocument>(`${PB}/documents`, body);
      this.doc = { id: d.id, title: d.title, dirty: false, savedAt: Date.now() };
      this.writeDraft();
      toast('success', opts.asCopy ? 'Saved a copy' : 'Painting saved', d.title);
      return true;
    } catch (e) {
      toastError(e, 'Could not save the painting');
      return false;
    } finally {
      this.saving = false;
    }
  }

  async open(id: string): Promise<boolean> {
    try {
      const d = await xapi().get<ExperimentDocument<PaintDocData>>(`${PB}/documents/${id}`);
      const loaded = await deserialize(d.data, this.materials);
      this.settings = { ...DEFAULT_SETTINGS, ...this.settings, ...loaded.settings };
      this.attach(loaded.canvas);
      this.doc = { id: d.id, title: d.title, dirty: false, savedAt: d.updated_at * 1000 };
      this.writeDraft();
      if (loaded.unknown.length) {
        toast('warning', 'Some materials are not available', `This engine has no ${loaded.unknown.join(', ')}; those areas were cleared.`);
      }
      return true;
    } catch (e) {
      toastError(e, 'Could not open the painting');
      return false;
    }
  }

  // --------------------------------------------------------------- drafts
  private scheduleDraft() {
    if (this.draftTimer) clearTimeout(this.draftTimer);
    this.draftTimer = setTimeout(() => {
      this.draftTimer = null;
      void this.writeDraft();
    }, 800);
  }

  /** Keeps the working painting in this desktop's storage so it survives restarts, saved or not. */
  async writeDraft(): Promise<void> {
    if (!this.canvas || !this.materials.length) return;
    try {
      const draft = { data: await this.data(), doc: $state.snapshot(this.doc), activeLayer: this.activeLayer,
                      material: this.material, brush: this.brush };
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      /* storage unavailable or full: the server copy (Save) still works */
    }
  }

  private async restoreDraft(): Promise<boolean> {
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return false;
      const d = JSON.parse(raw);
      const loaded = await deserialize(d.data, this.materials);
      this.settings = { ...DEFAULT_SETTINGS, ...loaded.settings };
      this.attach(loaded.canvas);
      this.doc = { id: d.doc?.id ?? null, title: d.doc?.title ?? 'Untitled painting', dirty: !!d.doc?.dirty, savedAt: d.doc?.savedAt ?? 0 };
      if (d.activeLayer && loaded.canvas.layer(d.activeLayer)) this.activeLayer = d.activeLayer;
      if (d.material) this.material = d.material;
      if (d.brush) this.brush = d.brush;
      return true;
    } catch {
      return false;
    }
  }

  /** Whether a working painting exists in this desktop's storage (used by the selection screen). */
  static hasDraft(): boolean {
    try {
      return !!localStorage.getItem(DRAFT_KEY);
    } catch {
      return false;
    }
  }
}
