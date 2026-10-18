// Shared experiment types (see engine/sarness_engine/experiments).

export interface Availability {
  state: 'ready' | 'partial' | 'needs_install' | 'unavailable';
  live?: boolean;
  refine?: boolean;
  reasons: string[];
}

export interface ExperimentSummary {
  id: string;
  name: string;
  tagline: string;
  description: string;
  version: string;
  capabilities: string[];
  availability: Availability;
  gpu_active: boolean;
}

export interface ExperimentDocument<T = unknown> {
  id: string;
  experiment_id: string;
  title: string;
  thumbnail: string | null;
  project_id: string | null;
  created_at: number;
  updated_at: number;
  data?: T;
}
