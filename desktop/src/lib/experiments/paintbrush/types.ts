// Wire types for /v1/experiments/gods-paintbrush (see engine/sarness_engine/experiments/paintbrush).

import type { Availability } from '../types';

export interface Material {
  index: number;
  id: string;
  label: string;
  color: string;
  group: string;
  coco_id: number;
  coco_name: string;
  ade_index: number;
  ade_name: string;
  ade_color: string;
  prompt: string;
  live: 'native' | 'experimental';
  refine: 'native' | 'approximate';
  note: string;
}

export interface ModelComponent {
  id: string;
  name: string;
  stage: 'live' | 'refine';
  description: string;
  license: string;
  source: string;
  url: string;
  size_bytes: number;
  installed: boolean;
}

export interface InstallState {
  running: boolean;
  component: string | null;
  done_bytes: number;
  total_bytes: number;
  error: string | null;
  finished_at: number | null;
}

export interface Runtime {
  worker: boolean;
  pid: number | null;
  device: string | null;
  loaded: { live: boolean; refine: boolean };
  vram_allocated_mb: number | null;
  vram_reserved_mb: number | null;
  load_s: Record<string, number>;
  busy: boolean;
  running: string | null;
  queued: number;
  gpu_busy: string | null;
  idle_s: number;
  last_error: string | null;
}

export interface CanvasSize {
  id: string;
  label: string;
  width: number;
  height: number;
}

export interface StageStats {
  count: number;
  median_total_ms: number;
  median_infer_ms: number;
  max_peak_vram_mb: number;
}

export interface PaintbrushStatus {
  id: string;
  name: string;
  tagline: string;
  description: string;
  availability: Availability;
  components: ModelComponent[];
  install: InstallState;
  runtime: Runtime;
  stats: Partial<Record<'live' | 'refine', StageStats>>;
  gpu: { name: string | null; vram_total_mb: number | null; vram_used_mb: number | null; vram_free_mb: number | null; utilization_pct: number | null };
  materials: Material[];
  canvas: { sizes: CanvasSize[]; min_side: number; max_side: number; multiple: number };
  refine: {
    scales: number[];
    max_pixels: number;
    quality: Record<Quality, number>;
    defaults: { quality: Quality; creativity: number; control: number; scale: number };
  };
  models: Record<'live' | 'refine', { id: string; name: string; labels: string }>;
}

export type Quality = 'draft' | 'standard' | 'high';

export interface RenderTimings {
  load_ms?: number;
  live_ms?: number;
  refine_ms?: number;
  queue_ms: number;
  total_ms: number;
}

export interface RenderResult {
  render_id: string;
  session: string;
  kind: 'live' | 'refine';
  width: number;
  height: number;
  mime: string;
  image: string;
  timings: RenderTimings;
  peak_vram_mb: number | null;
  coverage: Record<string, number>;
  model: string;
  seed?: number;
  prompt?: string;
  guidance?: string;
  quality?: Quality;
  steps?: number;
  creativity?: number;
  control?: number;
  scale?: number;
}

export type MaskFormat = 'color' | 'coco' | 'ade' | 'index';

export interface MaskExport {
  file_id: string;
  url: string;
  format: MaskFormat;
  name: string;
  legend: { material: string; label: string; value: number | string }[];
}
