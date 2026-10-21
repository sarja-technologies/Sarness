import { describe, expect, it } from 'vitest';
import type { InstalledModel } from '../api/types';
import { SEED_MAX } from '../util/seed';
import { applyImageModel, applyVideoModel, editsReference, imageDefaults, imageDims, imageSimpleParams, isMediaMode, MODE_ORDER, mediaBlocker, placeholderFor, sanitizeMode, stepMode, summaryFor, videoDefaults, videoSimpleParams } from './params';

const imgModel = { id: 'm-img', capabilities: { negative_prompt: false, lora: false }, defaults: { width: 1024, height: 1024, steps: 8, guidance: 0 }, limits: {} } as unknown as InstalledModel;
const vidModel = { id: 'm-vid', capabilities: { end_frame: false }, defaults: { width: 832, height: 480, steps: 20, frames: 33, fps: 24 }, limits: {} } as unknown as InstalledModel;

describe('modes', () => {
  it('has exactly chat, code, image, video in order', () => expect(MODE_ORDER).toEqual(['chat', 'code', 'image', 'video']));
  it('steps through modes with wrap-around', () => {
    expect(stepMode('chat', 1)).toBe('code');
    expect(stepMode('video', 1)).toBe('chat');
    expect(stepMode('chat', -1)).toBe('video');
  });
  it('sanitises unknown persisted values to chat', () => {
    expect(sanitizeMode('image')).toBe('image');
    expect(sanitizeMode('images')).toBe('chat');
    expect(sanitizeMode(undefined)).toBe('chat');
  });
  it('classifies media modes', () => {
    expect(isMediaMode('image') && isMediaMode('video')).toBe(true);
    expect(isMediaMode('chat') || isMediaMode('code')).toBe(false);
  });
  it('gives each mode its own prompt placeholder', () => {
    const p = MODE_ORDER.map((m) => placeholderFor(m, 'Qwen'));
    expect(new Set(p).size).toBe(4);
  });
});

describe('mode transitions keep each mode\'s settings independent', () => {
  it('changing image settings does not touch video settings', () => {
    const s = { image: imageDefaults(), video: videoDefaults() };
    applyImageModel(s.image, imgModel);
    s.image.aspect = '16:9';
    applyVideoModel(s.video, vidModel);
    expect(s.image.steps).toBe(8);
    expect(s.video.steps).toBe(20);
    expect(s.video.resolution).toBe('832x480');
    expect(s.image.aspect).toBe('16:9');
  });
  it('summary reflects the active mode only', () => {
    const s = { image: imageDefaults(), video: videoDefaults() };
    applyImageModel(s.image, imgModel);
    applyVideoModel(s.video, vidModel);
    expect(summaryFor('image', s, imgModel).join(' ')).toContain('steps');
    expect(summaryFor('video', s, vidModel).join(' ')).toContain('frames');
    expect(summaryFor('chat', s)).not.toContain('steps');
  });
});

describe('request bodies', () => {
  it('image: drops unsupported options and uses safe seeds', () => {
    const s = imageDefaults();
    applyImageModel(s, imgModel);
    s.negative_prompt = 'blurry';
    s.loras = [{ id: 'x', strength: 1 }];
    const body = imageSimpleParams(s, '  a cat ', imgModel);
    expect(body.prompt).toBe('a cat');
    expect(body.negative_prompt).toBe(''); // model does not support it: not silently sent
    expect(body.loras).toEqual([]);
    expect(body.seed).toBeGreaterThanOrEqual(0);
    expect(body.seed).toBeLessThanOrEqual(SEED_MAX);
    expect(body.model).toBe('m-img');
  });
  it('image: keeps an explicit seed and the aspect-derived size', () => {
    const s = imageDefaults();
    s.model = 'm-img';
    s.random_seed = false;
    s.seed = 1234;
    s.aspect = '16:9';
    const body = imageSimpleParams(s, 'x', imgModel);
    expect(body.seed).toBe(1234);
    const [w, h] = imageDims(s, imgModel);
    expect([body.width, body.height]).toEqual([w, h]);
    expect(w).toBeGreaterThan(h);
  });
  it('image: only sends a mask with a reference image', () => {
    const s = imageDefaults();
    s.mask_image = 'f_mask';
    expect(imageSimpleParams(s, 'x', imgModel).mask_image).toBeNull();
    s.reference_image = 'f_ref';
    expect(imageSimpleParams(s, 'x', imgModel).mask_image).toBe('f_mask');
  });
  it('video: text-to-video never carries frames; i2v needs model end-frame support', () => {
    const s = videoDefaults();
    applyVideoModel(s, vidModel);
    s.start_image = 'f1';
    s.end_image = 'f2';
    expect(videoSimpleParams(s, 'x', vidModel).start_image).toBeNull();
    s.vmode = 'i2v';
    const b = videoSimpleParams(s, 'x', vidModel);
    expect(b.start_image).toBe('f1');
    expect(b.end_image).toBeNull(); // model has no end_frame
    expect([b.width, b.height, b.frames, b.fps]).toEqual([832, 480, 33, 24]);
  });
  it('blocks unsendable media requests with a reason', () => {
    const s = imageDefaults();
    expect(mediaBlocker('image', 'hi', s)).toMatch(/model/);
    s.model = 'm';
    expect(mediaBlocker('image', '  ', s)).toMatch(/prompt/);
    expect(mediaBlocker('image', 'hi', s)).toBeNull();
    const v = videoDefaults();
    v.model = 'm';
    v.vmode = 'i2v';
    expect(mediaBlocker('video', 'hi', v)).toMatch(/start frame/);
  });
});

describe('editing models and styles', () => {
  const editModel = { id: 'qwen-image-edit-2511', capabilities: { modes: ['edit'], references: 3, negative_prompt: true }, defaults: {}, limits: {} } as unknown as InstalledModel;
  const kleinModel = { id: 'flux2-klein-4b', capabilities: { modes: ['txt2img', 'edit', 'inpaint'], references: 4 }, defaults: {}, limits: {} } as unknown as InstalledModel;

  it('sends the style for any model', () => {
    const s = imageDefaults();
    s.model = 'm-img';
    s.style = 'iphone';
    expect(imageSimpleParams(s, 'a dog', imgModel).style).toBe('iphone');
    expect(imageSimpleParams(imageDefaults(), 'a dog', imgModel).style).toBe('none');
  });

  it('sends extra references only with a reference image and only as many as the model takes', () => {
    const s = imageDefaults();
    s.extra_references = ['b', 'c', 'd'];
    expect(imageSimpleParams(s, 'x', editModel).extra_references).toEqual([]);
    s.reference_image = 'a';
    expect(imageSimpleParams(s, 'x', editModel).extra_references).toEqual(['b', 'c']);
    expect(imageSimpleParams(s, 'x', imgModel).extra_references).toEqual([], 'a model without editing takes none');
  });

  it('tells edit-only models apart and blocks them without an image to edit', () => {
    expect(editsReference(editModel)).toBe(true);
    expect(editsReference(kleinModel)).toBe(true);
    expect(editsReference(imgModel)).toBe(false);
    const s = imageDefaults();
    s.model = 'qwen-image-edit-2511';
    expect(mediaBlocker('image', 'make it night', s, editModel)).toMatch(/image to edit/);
    expect(mediaBlocker('image', 'a cat', s, kleinModel)).toBeNull();
    s.reference_image = 'a';
    expect(mediaBlocker('image', 'make it night', s, editModel)).toBeNull();
  });

  it('summarises style and edits honestly', () => {
    const s = { image: imageDefaults(), video: videoDefaults() };
    s.image.style = 'iphone';
    s.image.reference_image = 'a';
    s.image.extra_references = ['b'];
    const sum = summaryFor('image', s, editModel);
    expect(sum[0]).toBe('iPhone / candid photo');
    expect(sum.at(-1)).toBe('edit · 2 images');
  });
});
