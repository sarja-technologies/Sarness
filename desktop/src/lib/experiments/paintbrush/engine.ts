// God's Paintbrush painting engine: layers of material indices, compositing, brushes, fill and undo.
//
// Pixels hold material *indices* (1…N, see the engine's materials list), never colours: colours exist only for
// display. 0 means "unpainted" in a layer; where every visible layer is unpainted the background material shows.
// Brushes are hard-edged on purpose: a semantic label map has no in-between values.

export interface Layer {
  id: string;
  name: string;
  visible: boolean;
  data: Uint8Array;
}

/** Half-open pixel rectangle [x0, x1) × [y0, y1). */
export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface Snapshot {
  background: number;
  layers: Layer[];
}

type Step =
  | { kind: 'pixels'; layerId: string; rect: Rect; before: Uint8Array; after: Uint8Array }
  | { kind: 'structure'; before: Snapshot; after: Snapshot };

export const MAX_HISTORY = 60;

let seq = 0;
export const newLayerId = () => `l${Date.now().toString(36)}${(seq++).toString(36)}`;

export function unionRect(a: Rect | null, b: Rect | null): Rect | null {
  if (!a) return b;
  if (!b) return a;
  return { x0: Math.min(a.x0, b.x0), y0: Math.min(a.y0, b.y0), x1: Math.max(a.x1, b.x1), y1: Math.max(a.y1, b.y1) };
}

function cropRect(data: Uint8Array, w: number, r: Rect): Uint8Array {
  const rw = r.x1 - r.x0;
  const out = new Uint8Array(rw * (r.y1 - r.y0));
  for (let y = r.y0; y < r.y1; y++) out.set(data.subarray(y * w + r.x0, y * w + r.x1), (y - r.y0) * rw);
  return out;
}

function pasteRect(data: Uint8Array, w: number, r: Rect, src: Uint8Array) {
  const rw = r.x1 - r.x0;
  for (let y = r.y0; y < r.y1; y++) data.set(src.subarray((y - r.y0) * rw, (y - r.y0 + 1) * rw), y * w + r.x0);
}

export class LabelCanvas {
  readonly w: number;
  readonly h: number;
  background: number;
  layers: Layer[];
  /** Composite label map (what the model receives). */
  readonly comp: Uint8Array;
  private undoStack: Step[] = [];
  private redoStack: Step[] = [];
  private stroke: { layer: Layer; before: Uint8Array; rect: Rect | null } | null = null;

  constructor(w: number, h: number, background: number, layers?: Layer[]) {
    this.w = w;
    this.h = h;
    this.background = background;
    this.layers = layers?.length ? layers : [{ id: newLayerId(), name: 'Base', visible: true, data: new Uint8Array(w * h) }];
    this.comp = new Uint8Array(w * h);
    this.recomposite();
  }

  get full(): Rect {
    return { x0: 0, y0: 0, x1: this.w, y1: this.h };
  }

  layer(id: string): Layer | undefined {
    return this.layers.find((l) => l.id === id);
  }

  /** Recomputes the composite inside `r` (top layer wins, background where nothing is painted). */
  recomposite(r: Rect = this.full): void {
    const { w, comp, background } = this;
    const vis = this.layers.filter((l) => l.visible).map((l) => l.data).reverse();
    for (let y = r.y0; y < r.y1; y++) {
      for (let i = y * w + r.x0, end = y * w + r.x1; i < end; i++) {
        let v = 0;
        for (let k = 0; k < vis.length && !v; k++) v = vis[k][i];
        comp[i] = v || background;
      }
    }
  }

  pick(x: number, y: number): number {
    const xi = Math.floor(x);
    const yi = Math.floor(y);
    if (xi < 0 || yi < 0 || xi >= this.w || yi >= this.h) return 0;
    return this.comp[yi * this.w + xi];
  }

  // ------------------------------------------------------------ strokes
  beginStroke(layerId: string): boolean {
    const layer = this.layer(layerId);
    if (!layer) return false;
    this.stroke = { layer, before: layer.data.slice(), rect: null };
    return true;
  }

  /** Hard round dab of radius `r` (pixels) centred on (cx, cy). Returns the dirty rect. */
  dab(cx: number, cy: number, r: number, value: number): Rect | null {
    const s = this.stroke;
    if (!s) return null;
    const { w, h } = this;
    const rr = Math.max(0.5, r);
    const y0 = Math.max(0, Math.floor(cy - rr));
    const y1 = Math.min(h, Math.ceil(cy + rr));
    if (y0 >= y1) return null;
    let dirty: Rect | null = null;
    const data = s.layer.data;
    for (let y = y0; y < y1; y++) {
      const dy = y + 0.5 - cy;
      const span = rr * rr - dy * dy;
      if (span < 0) continue;
      const dx = Math.sqrt(span);
      const x0 = Math.max(0, Math.round(cx - dx));
      const x1 = Math.min(w, Math.round(cx + dx));
      if (x1 <= x0) continue;
      data.fill(value, y * w + x0, y * w + x1);
      dirty = unionRect(dirty, { x0, y0: y, x1, y1: y + 1 });
    }
    if (dirty) {
      s.rect = unionRect(s.rect, dirty);
      this.recomposite(dirty);
    }
    return dirty;
  }

  /** Dabs along a segment, spaced so the stroke has no gaps. */
  line(x0: number, y0: number, x1: number, y1: number, r: number, value: number): Rect | null {
    const dist = Math.hypot(x1 - x0, y1 - y0);
    const steps = Math.max(1, Math.ceil(dist / Math.max(0.75, r * 0.4)));
    let dirty: Rect | null = null;
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      dirty = unionRect(dirty, this.dab(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, r, value));
    }
    return dirty;
  }

  /** Ends the stroke and records one undo step. Returns whether anything changed. */
  endStroke(): boolean {
    const s = this.stroke;
    this.stroke = null;
    if (!s || !s.rect) return false;
    const before = cropRect(s.before, this.w, s.rect);
    const after = cropRect(s.layer.data, this.w, s.rect);
    if (before.every((v, i) => v === after[i])) return false;
    this.push({ kind: 'pixels', layerId: s.layer.id, rect: s.rect, before, after });
    return true;
  }

  /** Fills the connected region of the composite under (x, y) with `value`, written into the given layer. */
  fill(layerId: string, x: number, y: number, value: number): Rect | null {
    const layer = this.layer(layerId);
    const xi = Math.floor(x);
    const yi = Math.floor(y);
    if (!layer || xi < 0 || yi < 0 || xi >= this.w || yi >= this.h) return null;
    const { w, h, comp } = this;
    const target = comp[yi * w + xi];
    const data = layer.data;
    const before = data.slice();
    // Span fill: every pixel is filled once and every span scans its two neighbour rows once.
    const seen = new Uint8Array(w * h);
    const open = (j: number) => !seen[j] && comp[j] === target;
    const stack = [xi, yi];
    let rect: Rect | null = null;
    while (stack.length) {
      const py = stack.pop()!;
      const px = stack.pop()!;
      const row = py * w;
      if (seen[row + px]) continue;
      let lx = px;
      let rx = px;
      while (lx > 0 && open(row + lx - 1)) lx--;
      while (rx < w - 1 && open(row + rx + 1)) rx++;
      seen.fill(1, row + lx, row + rx + 1);
      data.fill(value, row + lx, row + rx + 1);
      rect = unionRect(rect, { x0: lx, y0: py, x1: rx + 1, y1: py + 1 });
      for (const ny of [py - 1, py + 1]) {
        if (ny < 0 || ny >= h) continue;
        let nx = lx;
        while (nx <= rx) {
          if (open(ny * w + nx)) {
            stack.push(nx, ny);
            while (nx <= rx && open(ny * w + nx)) nx++;
          } else nx++;
        }
      }
    }
    if (!rect) return null;
    const b = cropRect(before, w, rect);
    const a = cropRect(data, w, rect);
    if (b.every((v, i) => v === a[i])) return null;
    this.push({ kind: 'pixels', layerId, rect, before: b, after: a });
    this.recomposite(rect);
    return rect;
  }

  // -------------------------------------------------------- structure
  private snapshot(): Snapshot {
    return { background: this.background, layers: this.layers.map((l) => ({ ...l, data: l.data.slice() })) };
  }

  private restore(s: Snapshot) {
    this.background = s.background;
    this.layers = s.layers.map((l) => ({ ...l, data: l.data.slice() }));
    this.recomposite();
  }

  /** Applies a structural change (layers added/removed/reordered/cleared, background) as one undo step. */
  structural(change: (c: LabelCanvas) => void): void {
    const before = this.snapshot();
    change(this);
    this.recomposite();
    this.push({ kind: 'structure', before, after: this.snapshot() });
  }

  addLayer(name: string, above?: string): Layer {
    const layer: Layer = { id: newLayerId(), name, visible: true, data: new Uint8Array(this.w * this.h) };
    this.structural((c) => {
      const i = above ? c.layers.findIndex((l) => l.id === above) : c.layers.length - 1;
      c.layers.splice(i + 1, 0, layer);
    });
    return this.layers.find((l) => l.id === layer.id)!;
  }

  removeLayer(id: string): void {
    if (this.layers.length <= 1) return;
    this.structural((c) => {
      c.layers = c.layers.filter((l) => l.id !== id);
    });
  }

  moveLayer(id: string, delta: number): void {
    const i = this.layers.findIndex((l) => l.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= this.layers.length) return;
    this.structural((c) => {
      const [l] = c.layers.splice(i, 1);
      c.layers.splice(j, 0, l);
    });
  }

  setVisible(id: string, visible: boolean): void {
    this.structural((c) => {
      const l = c.layer(id);
      if (l) l.visible = visible;
    });
  }

  rename(id: string, name: string): void {
    this.structural((c) => {
      const l = c.layer(id);
      if (l) l.name = name;
    });
  }

  /** Clears one layer, or every layer and resets the background when `id` is omitted. */
  clear(id?: string, background?: number): void {
    this.structural((c) => {
      for (const l of c.layers) if (!id || l.id === id) l.data.fill(0);
      if (!id && background) c.background = background;
    });
  }

  // ----------------------------------------------------------- history
  private push(step: Step) {
    this.undoStack.push(step);
    if (this.undoStack.length > MAX_HISTORY) this.undoStack.shift();
    this.redoStack = [];
  }

  get canUndo() {
    return this.undoStack.length > 0;
  }

  get canRedo() {
    return this.redoStack.length > 0;
  }

  undo(): boolean {
    const s = this.undoStack.pop();
    if (!s) return false;
    this.apply(s, 'before');
    this.redoStack.push(s);
    return true;
  }

  redo(): boolean {
    const s = this.redoStack.pop();
    if (!s) return false;
    this.apply(s, 'after');
    this.undoStack.push(s);
    return true;
  }

  private apply(s: Step, which: 'before' | 'after') {
    if (s.kind === 'structure') {
      this.restore(s[which]);
      return;
    }
    const l = this.layer(s.layerId);
    if (!l) return;
    pasteRect(l.data, this.w, s.rect, s[which]);
    this.recomposite(s.rect);
  }

  /** Material → pixel share of the composite (indices with at least one pixel). */
  coverage(): Map<number, number> {
    const counts = new Map<number, number>();
    for (const v of this.comp) counts.set(v, (counts.get(v) ?? 0) + 1);
    const out = new Map<number, number>();
    for (const [k, n] of counts) out.set(k, n / this.comp.length);
    return out;
  }
}

// -------------------------------------------------------------- display
export type ColorLut = Uint8ClampedArray; // 256 × RGBA

export function colorLut(colors: Record<number, string>): ColorLut {
  const lut = new Uint8ClampedArray(256 * 4);
  for (const [k, hex] of Object.entries(colors)) {
    const h = hex.replace('#', '');
    const i = Number(k) * 4;
    lut[i] = parseInt(h.slice(0, 2), 16);
    lut[i + 1] = parseInt(h.slice(2, 4), 16);
    lut[i + 2] = parseInt(h.slice(4, 6), 16);
    lut[i + 3] = 255;
  }
  return lut;
}

/** Writes the composite colours of `r` into an RGBA buffer the size of the canvas. */
export function paintRgba(comp: Uint8Array, w: number, r: Rect, lut: ColorLut, out: Uint8ClampedArray): void {
  for (let y = r.y0; y < r.y1; y++) {
    for (let i = y * w + r.x0, end = y * w + r.x1; i < end; i++) {
      const c = comp[i] * 4;
      const o = i * 4;
      out[o] = lut[c];
      out[o + 1] = lut[c + 1];
      out[o + 2] = lut[c + 2];
      out[o + 3] = 255;
    }
  }
}

// ------------------------------------------------------------- encoding
export function toBase64(bytes: Uint8Array): string {
  let s = '';
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) s += String.fromCharCode(...bytes.subarray(i, i + CH));
  return btoa(s);
}

export function fromBase64(b64: string): Uint8Array {
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

async function pipe(bytes: Uint8Array, stream: CompressionStream | DecompressionStream): Promise<Uint8Array> {
  const out = new Response(new Blob([bytes as BlobPart]).stream().pipeThrough(stream));
  return new Uint8Array(await out.arrayBuffer());
}

export const canDeflate = () => typeof CompressionStream !== 'undefined';

/** zlib-wrapped deflate (what the engine expects for "deflate"). */
export const deflate = (bytes: Uint8Array) => pipe(bytes, new CompressionStream('deflate'));
export const inflate = (bytes: Uint8Array) => pipe(bytes, new DecompressionStream('deflate'));

export interface WireMask {
  width: number;
  height: number;
  encoding: 'deflate' | 'raw';
  data: string;
}

export async function encodeMask(labels: Uint8Array, width: number, height: number): Promise<WireMask> {
  if (canDeflate()) return { width, height, encoding: 'deflate', data: toBase64(await deflate(labels)) };
  return { width, height, encoding: 'raw', data: toBase64(labels) };
}
