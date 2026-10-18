import { describe, expect, it } from 'vitest';
import { LabelCanvas, MAX_HISTORY, colorLut, encodeMask, fromBase64, inflate, paintRgba, toBase64 } from './engine';

const SKY = 1;
const GRASS = 7;
const WATER = 4;
const TREES = 8;

function count(c: LabelCanvas, v: number) {
  return c.comp.reduce((n, x) => n + (x === v ? 1 : 0), 0);
}

describe('LabelCanvas', () => {
  it('starts as the background material everywhere', () => {
    const c = new LabelCanvas(64, 32, SKY);
    expect(c.comp.length).toBe(64 * 32);
    expect(count(c, SKY)).toBe(64 * 32);
    expect(c.layers).toHaveLength(1);
  });

  it('paints hard-edged dabs with exact material indices (no blending)', () => {
    const c = new LabelCanvas(64, 64, SKY);
    c.beginStroke(c.layers[0].id);
    const r = c.dab(32, 32, 10, GRASS)!;
    expect(c.endStroke()).toBe(true);
    expect(r.x0).toBeGreaterThanOrEqual(21);
    expect(r.x1).toBeLessThanOrEqual(43);
    expect(new Set(c.comp)).toEqual(new Set([SKY, GRASS]));
    expect(c.pick(32, 32)).toBe(GRASS);
    expect(c.pick(32, 20)).toBe(SKY); // outside radius 10 at dy = 12
    const area = count(c, GRASS);
    expect(area).toBeGreaterThan(Math.PI * 100 * 0.85);
    expect(area).toBeLessThan(Math.PI * 100 * 1.15);
  });

  it('draws gap-free lines and clips at the edges', () => {
    const c = new LabelCanvas(100, 20, SKY);
    c.beginStroke(c.layers[0].id);
    c.line(-10, 10, 110, 10, 2, WATER);
    c.endStroke();
    for (let x = 0; x < 100; x++) expect(c.pick(x, 10)).toBe(WATER);
  });

  it('flood-fills the connected region of the composite only', () => {
    const c = new LabelCanvas(40, 40, SKY);
    const base = c.layers[0];
    // a wall of grass splits the canvas in two
    for (let y = 0; y < 40; y++) base.data[y * 40 + 20] = GRASS;
    c.recomposite();
    const r = c.fill(base.id, 5, 5, WATER)!;
    expect(r).toEqual({ x0: 0, y0: 0, x1: 20, y1: 40 });
    expect(c.pick(5, 5)).toBe(WATER);
    expect(c.pick(30, 5)).toBe(SKY);
    expect(c.pick(20, 5)).toBe(GRASS);
    expect(c.fill(base.id, 5, 5, WATER)).toBeNull(); // nothing to change
  });

  it('fills large regions in linear time', () => {
    const c = new LabelCanvas(1024, 1024, SKY);
    const t0 = performance.now();
    c.fill(c.layers[0].id, 500, 500, GRASS);
    expect(performance.now() - t0).toBeLessThan(1500);
    expect(count(c, GRASS)).toBe(1024 * 1024);
  });

  it('composites layers top-down and respects visibility', () => {
    const c = new LabelCanvas(10, 10, SKY);
    c.layers[0].data.fill(GRASS);
    const top = c.addLayer('Trees');
    top.data[0] = TREES;
    c.recomposite();
    expect(c.pick(0, 0)).toBe(TREES);
    expect(c.pick(1, 0)).toBe(GRASS);
    c.setVisible(top.id, false);
    expect(c.pick(0, 0)).toBe(GRASS);
    c.moveLayer(top.id, -1);
    c.setVisible(top.id, true);
    expect(c.layers[0].id).toBe(top.id);
    expect(c.pick(0, 0)).toBe(GRASS); // the full grass layer is now on top
  });

  it('erasing reveals lower layers, then the background', () => {
    const c = new LabelCanvas(10, 10, SKY);
    c.layers[0].data.fill(GRASS);
    const top = c.addLayer('Water');
    top.data.fill(WATER);
    c.recomposite();
    c.beginStroke(top.id);
    c.dab(5, 5, 1, 0);
    c.endStroke();
    expect(c.pick(5, 5)).toBe(GRASS);
    c.beginStroke(c.layers[0].id);
    c.dab(5, 5, 1, 0);
    c.endStroke();
    expect(c.pick(5, 5)).toBe(SKY);
  });

  it('undoes and redoes strokes, fills and structural changes', () => {
    const c = new LabelCanvas(32, 32, SKY);
    const id = c.layers[0].id;
    c.beginStroke(id);
    c.line(0, 16, 31, 16, 3, GRASS);
    c.endStroke();
    const afterStroke = c.comp.slice();
    c.fill(id, 1, 1, WATER);
    c.addLayer('Extra');
    expect(c.layers).toHaveLength(2);
    expect(c.undo()).toBe(true);
    expect(c.layers).toHaveLength(1);
    expect(c.undo()).toBe(true);
    expect(c.comp).toEqual(afterStroke);
    expect(c.undo()).toBe(true);
    expect(count(c, SKY)).toBe(32 * 32);
    expect(c.undo()).toBe(false);
    expect(c.redo() && c.redo()).toBe(true);
    expect(c.pick(1, 1)).toBe(WATER);
    // a new action clears the redo stack
    c.clear();
    expect(c.canRedo).toBe(false);
    expect(count(c, SKY)).toBe(32 * 32);
  });

  it('caps the history', () => {
    const c = new LabelCanvas(8, 8, SKY);
    for (let i = 0; i < MAX_HISTORY + 10; i++) {
      c.beginStroke(c.layers[0].id);
      c.dab(4, 4, 2, i % 2 ? GRASS : WATER);
      c.endStroke();
    }
    let n = 0;
    while (c.undo()) n++;
    expect(n).toBe(MAX_HISTORY);
  });

  it('a stroke that changes nothing records no undo step', () => {
    const c = new LabelCanvas(8, 8, SKY);
    c.layers[0].data.fill(GRASS);
    c.recomposite();
    c.beginStroke(c.layers[0].id);
    c.dab(4, 4, 2, GRASS);
    expect(c.endStroke()).toBe(false);
    expect(c.canUndo).toBe(false);
  });
});

describe('display and wire encoding', () => {
  it('maps material indices to their interface colours', () => {
    const c = new LabelCanvas(2, 1, SKY);
    c.layers[0].data[1] = GRASS;
    c.recomposite();
    const out = new Uint8ClampedArray(2 * 4);
    paintRgba(c.comp, 2, c.full, colorLut({ [SKY]: '#8ec5f4', [GRASS]: '#79c25a' }), out);
    expect([...out]).toEqual([0x8e, 0xc5, 0xf4, 255, 0x79, 0xc2, 0x5a, 255]);
  });

  it('base64 round-trips large buffers', () => {
    const bytes = new Uint8Array(300_000).map((_, i) => i % 251);
    expect(fromBase64(toBase64(bytes))).toEqual(bytes);
  });

  it('encodes masks as zlib deflate the engine can read back exactly', async () => {
    const c = new LabelCanvas(64, 32, SKY);
    c.layers[0].data.fill(GRASS, 64 * 16);
    c.recomposite();
    const m = await encodeMask(c.comp, 64, 32);
    expect(m).toMatchObject({ width: 64, height: 32, encoding: 'deflate' });
    const raw = fromBase64(m.data);
    expect(raw[0]).toBe(0x78); // zlib header, as Python's zlib.decompress expects
    expect(await inflate(raw)).toEqual(c.comp);
    expect(raw.length).toBeLessThan(200);
  });
});
