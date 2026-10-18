import { describe, expect, it } from 'vitest';
import { DEFAULT_SETTINGS } from './session.svelte';
import { DocError, deserialize, serialize } from './doc';
import { LabelCanvas } from './engine';
import type { Material } from './types';

const mat = (index: number, id: string, color = '#000000'): Material => ({
  index, id, label: id, color, group: 'x', coco_id: 0, coco_name: '', ade_index: 0, ade_name: '', ade_color: '',
  prompt: id, live: 'native', refine: 'native', note: '',
});

const MATS = [mat(1, 'sky'), mat(2, 'grass'), mat(3, 'water'), mat(4, 'snow')];

function painting() {
  const c = new LabelCanvas(32, 16, 1);
  c.layers[0].data.fill(2, 32 * 8);
  const top = c.addLayer('Lake');
  top.data.fill(3, 32 * 12, 32 * 12 + 10);
  c.setVisible(top.id, true);
  return c;
}

describe('painting documents', () => {
  it('round-trips layers, background and settings', async () => {
    const c = painting();
    const settings = { ...DEFAULT_SETTINGS, seed: 42, guidance: 'golden hour' };
    const data = await serialize(c, MATS, settings, { live: 'a', refine: 'b' });
    expect(data.format).toBe('sarness.paintbrush');
    expect(data.layers.map((l) => l.name)).toEqual(['Base', 'Lake']);
    expect(JSON.stringify(data).length).toBeLessThan(2000); // compressed, not raw pixels
    const back = await deserialize(JSON.parse(JSON.stringify(data)), MATS);
    expect(back.canvas.comp).toEqual(c.comp);
    expect(back.canvas.layers.map((l) => l.name)).toEqual(['Base', 'Lake']);
    expect(back.settings).toMatchObject({ seed: 42, guidance: 'golden hour' });
    expect(back.unknown).toEqual([]);
  });

  it('remaps materials by id when the engine reorders them', async () => {
    const data = await serialize(painting(), MATS, DEFAULT_SETTINGS);
    const reordered = [mat(1, 'water'), mat(2, 'sky'), mat(3, 'grass')];
    const back = await deserialize(data, reordered);
    expect(back.canvas.background).toBe(2); // sky
    expect(back.canvas.pick(0, 15)).toBe(3); // grass
    expect(back.canvas.pick(1, 12)).toBe(1); // water
  });

  it('clears materials this engine does not know and reports them', async () => {
    const c = new LabelCanvas(8, 8, 1);
    c.layers[0].data.fill(4);
    c.recomposite();
    const back = await deserialize(await serialize(c, MATS, DEFAULT_SETTINGS), MATS.slice(0, 3));
    expect(back.unknown).toEqual(['snow']);
    expect(back.canvas.pick(3, 3)).toBe(1); // unpainted → background (sky)
  });

  it('rejects foreign or damaged documents', async () => {
    await expect(deserialize({ format: 'other' }, MATS)).rejects.toBeInstanceOf(DocError);
    const data = await serialize(painting(), MATS, DEFAULT_SETTINGS);
    await expect(deserialize({ ...data, version: 99 }, MATS)).rejects.toThrow(/newer version/);
    await expect(deserialize({ ...data, width: 31 }, MATS)).rejects.toThrow(/damaged/);
  });
});
