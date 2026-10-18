// Pure logic for the universal composer: modes, per-mode settings, and the
// request bodies sent to the engine. No UI and no runes, so it is unit-tested.
import type { InstalledModel } from '../api/types';
import { dimsFor } from '../studio/aspect';
import { seedFor } from '../util/seed';

export type Mode = 'chat' | 'code' | 'image' | 'video';
export const MODE_ORDER: Mode[] = ['chat', 'code', 'image', 'video'];
export const MODE_LABEL: Record<Mode, string> = { chat: 'Chat', code: 'Code', image: 'Image', video: 'Video' };
export const isMediaMode = (m: Mode): m is 'image' | 'video' => m === 'image' || m === 'video';
export const isTextMode = (m: Mode) => m === 'chat' || m === 'code';

export function placeholderFor(mode: Mode, modelName?: string | null): string {
  if (mode === 'chat') return modelName ? `Message ${modelName}…` : 'Choose a model to start';
  if (mode === 'code') return 'Describe the code you need, or paste code to explain or revise…';
  if (mode === 'image') return 'Describe the image you want to create…';
  return 'Describe the scene, subject and camera motion…';
}

export function sanitizeMode(v: unknown): Mode {
  return MODE_ORDER.includes(v as Mode) ? (v as Mode) : 'chat';
}

/** Cycle used by the composer's keyboard handling. */
export function stepMode(mode: Mode, dir: 1 | -1): Mode {
  return MODE_ORDER[(MODE_ORDER.indexOf(mode) + dir + MODE_ORDER.length) % MODE_ORDER.length];
}

// ------------------------------------------------------------------ image
export interface ImageSettings {
  model: string | null;
  negative_prompt: string;
  aspect: string;
  size: 'S' | 'M' | 'L';
  quality: 'fast' | 'balanced' | 'best';
  batch_size: number;
  seed: number;
  random_seed: boolean;
  steps: number;
  guidance: number;
  scheduler: string;
  denoise: number;
  reference_image: string | null;
  mask_image: string | null;
  loras: { id: string; strength: number }[];
  vae: string | null;
  memory_mode: string;
  upscale_model: string | null;
  upscale_factor: number;
  format: string;
  clip_skip: number;
}

export const imageDefaults = (): ImageSettings => ({
  model: null, negative_prompt: '', aspect: '1:1', size: 'M', quality: 'balanced', batch_size: 1, seed: 0, random_seed: true,
  steps: 30, guidance: 6, scheduler: 'default', denoise: 0.65, reference_image: null, mask_image: null, loras: [], vae: null,
  memory_mode: 'auto', upscale_model: null, upscale_factor: 2, format: 'png', clip_skip: 0,
});

export function imageDims(s: ImageSettings, model?: InstalledModel): [number, number] {
  const d = (model?.defaults ?? {}) as Record<string, any>;
  const lim = model?.limits ?? {};
  const baseArea = (d.width ?? 1024) * (d.height ?? 1024);
  const mul = { S: 0.75, M: 1, L: 1.25 }[s.size];
  return dimsFor(s.aspect, baseArea * mul * mul, 64, lim.min_side ?? 256, lim.max_side ?? 2048);
}

export function applyImageModel(s: ImageSettings, model?: InstalledModel) {
  s.model = model?.id ?? null;
  const d = (model?.defaults ?? {}) as Record<string, any>;
  if (d.steps) s.steps = d.steps;
  if (d.guidance !== undefined) s.guidance = d.guidance;
  s.scheduler = 'default';
  s.quality = 'balanced';
}

export function imageSimpleParams(s: ImageSettings, prompt: string, model?: InstalledModel, overrides: Record<string, unknown> = {}) {
  const caps = model?.capabilities ?? {};
  const [width, height] = imageDims(s, model);
  return {
    model: s.model,
    prompt: prompt.trim(),
    negative_prompt: caps.negative_prompt === false ? '' : s.negative_prompt.trim(),
    width,
    height,
    batch_size: s.batch_size,
    steps: s.steps,
    guidance: s.guidance,
    seed: seedFor(s.random_seed, s.seed),
    scheduler: s.scheduler,
    denoise: s.denoise,
    reference_image: s.reference_image,
    mask_image: s.reference_image ? s.mask_image : null,
    loras: caps.lora ? s.loras.filter((l) => l.id) : [],
    vae: s.vae,
    memory_mode: s.memory_mode,
    upscale: s.upscale_model ? { model: s.upscale_model, factor: s.upscale_factor } : null,
    format: s.format,
    clip_skip: s.clip_skip,
    ...overrides,
  };
}

// ------------------------------------------------------------------ video
export interface VideoSettings {
  model: string | null;
  vmode: 't2v' | 'i2v';
  negative_prompt: string | null;
  resolution: string;
  frames: number;
  fps: number;
  steps: number;
  guidance: number;
  seed: number;
  random_seed: boolean;
  flow_shift: number;
  start_image: string | null;
  end_image: string | null;
  memory_mode: string;
  format: string;
}

export const videoDefaults = (): VideoSettings => ({
  model: null, vmode: 't2v', negative_prompt: null, resolution: '', frames: 33, fps: 16, steps: 30, guidance: 5, seed: 0,
  random_seed: true, flow_shift: 3, start_image: null, end_image: null, memory_mode: 'auto', format: 'mp4',
});

export function videoDims(s: VideoSettings, model?: InstalledModel): [number, number] {
  const d = (model?.defaults ?? {}) as Record<string, any>;
  const [w, h] = (s.resolution || `${d.width ?? 832}x${d.height ?? 480}`).split('x').map(Number);
  return [w, h];
}

export function applyVideoModel(s: VideoSettings, model?: InstalledModel) {
  s.model = model?.id ?? null;
  const d = (model?.defaults ?? {}) as Record<string, any>;
  if (d.steps) s.steps = d.steps;
  if (d.guidance) s.guidance = d.guidance;
  if (d.frames) s.frames = d.frames;
  if (d.fps) s.fps = d.fps;
  if (d.flow_shift) s.flow_shift = d.flow_shift;
  s.resolution = d.width ? `${d.width}x${d.height}` : '';
}

export function videoSimpleParams(s: VideoSettings, prompt: string, model?: InstalledModel, overrides: Record<string, unknown> = {}) {
  const caps = model?.capabilities ?? {};
  const [width, height] = videoDims(s, model);
  return {
    model: s.model,
    prompt: prompt.trim(),
    negative_prompt: s.negative_prompt?.trim() || null,
    width,
    height,
    frames: s.frames,
    fps: s.fps,
    steps: s.steps,
    guidance: s.guidance,
    seed: seedFor(s.random_seed, s.seed),
    flow_shift: s.flow_shift,
    start_image: s.vmode === 'i2v' ? s.start_image : null,
    end_image: s.vmode === 'i2v' && caps.end_frame ? s.end_image : null,
    memory_mode: s.memory_mode,
    format: s.format,
    ...overrides,
  };
}

/** Why a media request cannot be sent yet, or null when it is ready. */
export function mediaBlocker(mode: 'image' | 'video', prompt: string, s: ImageSettings | VideoSettings): string | null {
  if (!s.model) return `Choose a ${mode} model first`;
  if (!prompt.trim()) return 'Write a prompt first';
  if (mode === 'video' && (s as VideoSettings).vmode === 'i2v' && !(s as VideoSettings).start_image) return 'Add a start frame for image-to-video';
  return null;
}

/** Short, honest summary of what the next send will use. Never invents values. */
export function summaryFor(mode: Mode, s: { image: ImageSettings; video: VideoSettings }, model?: InstalledModel, chat?: { temperature?: number; ctx?: number }): string[] {
  if (mode === 'image') {
    const [w, h] = imageDims(s.image, model);
    return [s.image.aspect, `${w}×${h}`, `${s.image.steps} steps`, s.image.random_seed ? 'random seed' : `seed ${s.image.seed}`, ...(s.image.reference_image ? [`edit ${s.image.denoise.toFixed(2)}`] : [])];
  }
  if (mode === 'video') {
    const [w, h] = videoDims(s.video, model);
    return [`${w}×${h}`, `${s.video.frames} frames · ${(s.video.frames / s.video.fps).toFixed(1)} s`, s.video.vmode === 'i2v' ? 'image→video' : 'text→video', s.video.random_seed ? 'random seed' : `seed ${s.video.seed}`];
  }
  return [mode === 'code' ? 'coding prompt' : 'chat', ...(chat?.temperature !== undefined ? [`temp ${chat.temperature}`] : [])];
}
