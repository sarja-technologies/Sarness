// Saved-painting format ("sarness.paintbrush" v1), stored as an experiment document on the engine.
//
// Layer pixels are material indices, so the document carries the palette it was painted with (index → material
// id). Loading remaps by id, which keeps old paintings correct if the engine ever reorders or adds materials.

import { LabelCanvas, deflate, fromBase64, inflate, toBase64, type Layer } from './engine';
import type { Material, Quality } from './types';

export const DOC_FORMAT = 'sarness.paintbrush';
export const DOC_VERSION = 1;

export interface PaintSettings {
  live: boolean;
  autoRefine: boolean;
  quality: Quality;
  creativity: number;
  control: number;
  scale: number;
  seed: number;
  guidance: string;
}

export interface PaintDocData {
  format: typeof DOC_FORMAT;
  version: number;
  width: number;
  height: number;
  background: string;
  palette: { index: number; id: string; color: string }[];
  layers: { id: string; name: string; visible: boolean; encoding: 'deflate' | 'raw'; data: string }[];
  settings: PaintSettings;
  model?: { live: string; refine: string };
}

export class DocError extends Error {}

export async function serialize(c: LabelCanvas, materials: Material[], settings: PaintSettings,
                                model?: PaintDocData['model']): Promise<PaintDocData> {
  const bg = materials.find((m) => m.index === c.background);
  const layers = [];
  for (const l of c.layers) {
    const packed = typeof CompressionStream !== 'undefined';
    layers.push({ id: l.id, name: l.name, visible: l.visible, encoding: (packed ? 'deflate' : 'raw') as 'deflate' | 'raw',
                  data: toBase64(packed ? await deflate(l.data) : l.data) });
  }
  return {
    format: DOC_FORMAT,
    version: DOC_VERSION,
    width: c.w,
    height: c.h,
    background: bg?.id ?? materials[0]?.id ?? 'sky',
    palette: materials.map((m) => ({ index: m.index, id: m.id, color: m.color })),
    layers,
    settings: { ...settings },
    model,
  };
}

export interface Loaded {
  canvas: LabelCanvas;
  settings: Partial<PaintSettings>;
  /** Material ids in the document that this engine does not know (painted pixels were cleared). */
  unknown: string[];
}

export async function deserialize(raw: unknown, materials: Material[]): Promise<Loaded> {
  const d = raw as PaintDocData;
  if (!d || d.format !== DOC_FORMAT) throw new DocError('This is not a God\'s Paintbrush painting.');
  if (d.version > DOC_VERSION) throw new DocError('This painting was saved by a newer version of Sarness.');
  const { width: w, height: h } = d;
  if (!Number.isInteger(w) || !Number.isInteger(h) || w < 1 || h < 1 || w * h > 4096 * 4096) {
    throw new DocError('The painting has an invalid size.');
  }
  const byId = new Map(materials.map((m) => [m.id, m.index]));
  const remap = new Uint8Array(256);
  const unknown = new Set<string>();
  for (const p of d.palette ?? []) {
    const idx = byId.get(p.id);
    if (idx) remap[p.index] = idx;
    else unknown.add(p.id);
  }
  const layers: Layer[] = [];
  for (const l of d.layers ?? []) {
    let bytes = fromBase64(l.data);
    if (l.encoding === 'deflate') bytes = await inflate(bytes);
    if (bytes.length !== w * h) throw new DocError(`Layer “${l.name}” is damaged.`);
    const data = new Uint8Array(w * h);
    for (let i = 0; i < bytes.length; i++) data[i] = bytes[i] ? remap[bytes[i]] : 0;
    layers.push({ id: l.id, name: l.name, visible: l.visible !== false, data });
  }
  if (!layers.length) throw new DocError('The painting has no layers.');
  const background = byId.get(d.background) ?? materials[0]?.index ?? 1;
  return { canvas: new LabelCanvas(w, h, background, layers), settings: d.settings ?? {}, unknown: [...unknown] };
}
