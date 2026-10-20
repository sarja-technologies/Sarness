// Typed contracts for the Sarness Engine API v1 (see docs/API.md).

export type ID = string;

export interface Health {
  status: 'ok';
  service: 'sarness-engine';
  api_version: string;
  engine_version: string;
  auth: string;
}

export interface GpuInfo {
  index: number;
  name: string;
  vram_total_mb: number;
  vram_used_mb: number;
  vram_free_mb: number;
  utilization_pct: number;
  temperature_c: number | null;
  power_w: number | null;
  power_limit_w?: number | null;
  driver_version?: string;
  cuda_driver_version?: string | null;
  /** The GPU shares system memory (DGX Spark GB10); memory figures are system memory. */
  unified_memory?: boolean;
  compute_capability?: string | null;
}

export interface SystemInfo {
  engine: { version: string; api_version: string; uptime_s: number; data_dir: string; outputs_dir: string; models_dir: string; bind: string };
  hardware: {
    hostname: string;
    os: string;
    /** x86_64 or aarch64 (engines from 0.5). */
    arch?: string;
    distro: string;
    python: string;
    cpu: { name: string; cores_physical: number; cores_logical: number; utilization_pct: number; load_avg: number[] };
    memory: { total_mb: number; used_mb: number; available_mb: number };
    storage: { path: string; total_gb: number; used_gb: number; free_gb: number };
    gpus: GpuInfo[];
  };
  runtime: {
    torch: string | null;
    torch_cuda: string | null;
    diffusers: string | null;
    transformers: string | null;
    cuda_driver: string | null;
    llama_cpp: string | null;
    worker: { alive: boolean; busy: boolean; info: Record<string, unknown>; loaded: LoadedModel[]; vram_allocated_mb: number | null };
  };
  llm: LlmStatus;
  jobs: Partial<Record<JobStatus, number>>;
  models: { installed: number; by_category: Record<string, number> };
  health: { status: 'ok' | 'degraded'; issues: string[] };
}

export interface LiveStats {
  gpus: Pick<GpuInfo, 'index' | 'vram_used_mb' | 'vram_free_mb' | 'vram_total_mb' | 'utilization_pct' | 'temperature_c' | 'power_w'>[];
  cpu_pct: number;
  ram_used_mb: number;
  ram_total_mb: number;
  worker_alive: boolean;
  llm: string;
}

export interface LoadedModel {
  key: string;
  kind: string;
  mode: string;
  vram_mb: number;
}

export type ModelCategory = 'llm' | 'image' | 'video' | 'upscaler' | 'vae' | 'lora' | 'controlnet';

export interface Compatibility {
  level: 'fits' | 'offload' | 'too_large' | 'unknown';
  label: string;
}

export interface ModelLimits {
  steps?: [number, number];
  guidance?: [number, number];
  min_side?: number;
  max_side?: number;
  multiple?: number;
  frames?: [number, number];
  frame_step?: number;
  fps?: [number, number];
  resolutions?: [number, number][];
}

export interface ModelCapabilities {
  modes?: string[];
  lora?: boolean;
  controlnet?: boolean;
  negative_prompt?: boolean;
  schedulers?: string[];
  start_frame?: boolean;
  end_frame?: boolean;
  motion?: string[];
  vision?: boolean;
  context_max?: number;
  default_ctx?: number;
  scale?: number;
  remote?: boolean;
  guidance?: boolean;
  steps?: boolean;
  denoise?: boolean;
  flow_shift?: boolean;
}

export interface InstalledModel {
  id: ID;
  catalog_id: string | null;
  name: string;
  category: ModelCategory;
  family: string | null;
  format: string;
  path: string;
  size: number | null;
  installed_at: number;
  supported: boolean;
  unsupported_reason?: string | null;
  remote?: boolean;
  description?: string;
  capabilities?: ModelCapabilities;
  defaults?: Record<string, number | string>;
  limits?: ModelLimits;
  vram_mb?: number | null;
  vram_offload_mb?: number | null;
  license?: string;
  tags?: string[];
  notes?: string;
  quantization?: string;
  loaded?: boolean;
  meta?: Record<string, unknown>;
  /** Server profile id hosting the model (set by the desktop). */
  engine?: string;
}

export interface Download {
  id: ID;
  model_id: string;
  catalog_id: string | null;
  name: string;
  category: ModelCategory;
  repo: string;
  dest: string;
  total_bytes: number;
  done_bytes: number;
  status: 'queued' | 'downloading' | 'paused' | 'completed' | 'failed' | 'cancelled';
  error: string | null;
  speed_bps?: number;
  created_at: number;
  /** Server profile id downloading it (set by the desktop). */
  engine?: string;
}

export interface CatalogQuant {
  id: string;
  files: string[];
  size_gb: number;
  vram_mb: number;
  default?: boolean;
  model_id: string;
  installed: boolean;
  download: Download | null;
  compatibility: Compatibility;
}

export interface CatalogEntry {
  id: string;
  name: string;
  category: ModelCategory;
  family: string;
  format: string;
  repo: string;
  description: string;
  size_gb?: number;
  vram_mb?: number;
  vram_offload_mb?: number;
  quantizations?: CatalogQuant[];
  installed: boolean;
  download?: Download | null;
  model_id?: string;
  compatibility: Compatibility;
  capabilities?: ModelCapabilities;
  license?: string;
  tags?: string[];
  notes?: string;
}

export interface HardwarePreset {
  id: string;
  name: string;
  vram_mb: number;
  recommended: string[];
  notes: string;
  matches_this_server: boolean;
}

export interface HfSearchResult {
  id: string;
  downloads: number;
  likes: number;
  pipeline_tag: string | null;
  library: string | null;
  tags: string[];
  updated: string;
  gated: boolean | string;
}

export interface HfOption {
  label: string;
  files: string[];
  size_bytes: number;
  vram_mb: number;
  vision?: boolean;
  compatibility: Compatibility;
}

export interface HfInspect {
  repo: string;
  format: 'gguf' | 'diffusers' | 'files';
  category: ModelCategory | null;
  pipeline?: string;
  supported?: boolean;
  unsupported_reason?: string;
  total_bytes: number;
  options: HfOption[];
}

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface NodeState {
  status: 'pending' | 'running' | 'done' | 'cached' | 'error' | 'skipped';
  message?: string;
  elapsed_s?: number;
}

export interface Job {
  id: ID;
  kind: 'image' | 'video';
  status: JobStatus;
  title: string;
  project_id: ID | null;
  folder_id: ID | null;
  progress: number;
  progress_text: string | null;
  current_node: string | null;
  node_states: Record<string, NodeState> | null;
  error: { message: string; node_id?: string | null; oom?: boolean; hint?: string | null } | null;
  attempts: number;
  vram_estimate_mb: number | null;
  retry_of: ID | null;
  result: { generation_ids: ID[]; elapsed_s: number; peak_vram_mb: number } | null;
  simple: Record<string, unknown> | null;
  workflow?: Workflow;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  queue_position?: number | null;
  waiting_reason?: string | null;
  generations?: Generation[];
  /** Idempotency key the desktop sent with the submission. */
  client_key?: string | null;
  /** Server profile id running the job (set by the desktop). */
  engine?: string;
}

export interface Generation {
  id: ID;
  job_id: ID | null;
  type: 'image' | 'video';
  project_id: ID | null;
  folder_id: ID | null;
  file_id: ID;
  thumb_file_id: ID | null;
  url: string;
  thumb_url: string;
  prompt: string | null;
  negative_prompt: string | null;
  model_id: string | null;
  seed: number | null;
  width: number | null;
  height: number | null;
  frames: number | null;
  fps: number | null;
  duration: number | null;
  metadata: Record<string, unknown>;
  simple: Record<string, unknown> | null;
  workflow?: Workflow;
  favorite: number;
  tags?: string[];
  collections?: ID[];
  file?: { mime: string; size: number; sha256: string; width: number; height: number; duration: number | null };
  path_on_server?: string;
  deleted_at: number | null;
  created_at: number;
  /** Server profile id that produced and stores it (set by the desktop). */
  engine?: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: number | null;
}

export interface Folder {
  id: ID;
  project_id: ID;
  parent_id: ID | null;
  name: string;
  sort: number;
  created_at: number;
}

export interface Project {
  id: ID;
  name: string;
  color: string | null;
  icon: string | null;
  sort: number;
  folders: Folder[];
  generation_count: number;
  created_at: number;
}

export interface Collection {
  id: ID;
  name: string;
  count: number;
  cover_url: string | null;
  created_at: number;
}

// ------------------------------------------------------------------ workflow
export interface WfNode {
  id: string;
  type: string;
  params: Record<string, unknown>;
  position: { x: number; y: number };
  label?: string;
  color?: string;
}

export interface WfEdge {
  id: string;
  source: string;
  source_output: string;
  target: string;
  target_input: string;
}

export interface WfGroup {
  id: string;
  label: string;
  color?: string;
  nodes: string[];
  rect?: { x: number; y: number; width: number; height: number };
}

export interface Workflow {
  format: 'sarness.workflow';
  version: number;
  name: string;
  kind: 'image' | 'video';
  nodes: WfNode[];
  edges: WfEdge[];
  groups: WfGroup[];
  simple?: Record<string, unknown>;
  ui?: { viewport?: { x: number; y: number; zoom: number } };
}

export interface SocketSpec {
  name: string;
  type: string;
  label: string;
  optional: boolean;
}

export interface ParamSpec {
  name: string;
  kind: 'int' | 'float' | 'string' | 'text' | 'bool' | 'enum' | 'seed' | 'model' | 'file' | 'color';
  default: unknown;
  label: string;
  min?: number;
  max?: number;
  step?: number;
  options?: (string | { value: string; label: string })[];
  model_category?: ModelCategory;
  model_family?: string[];
  file_accept?: 'image' | 'video';
  socket?: boolean;
  socket_type?: string;
  advanced?: boolean;
  description?: string;
  optional?: boolean;
}

export interface NodeSpec {
  type: string;
  title: string;
  category: string;
  description: string;
  inputs: SocketSpec[];
  outputs: SocketSpec[];
  params: ParamSpec[];
  output_node: boolean;
  gpu: boolean;
  passthrough: boolean;
  tags: string[];
}

export interface NodeLibrary {
  format: string;
  version: number;
  socket_types: { type: string; color: string }[];
  categories: string[];
  nodes: NodeSpec[];
}

export interface ValidationIssue {
  message: string;
  node_id: string | null;
  edge_id: string | null;
  level: 'error' | 'warning';
}

export interface SavedWorkflow {
  id: ID;
  name: string;
  kind: 'image' | 'video';
  description: string;
  is_preset: number;
  graph?: Workflow;
  created_at: number;
  updated_at: number;
}

// ---------------------------------------------------------------------- chat
export interface LlmStatus {
  state: 'stopped' | 'loading' | 'ready' | 'error';
  error: string | null;
  model_id: string | null;
  model_name: string | null;
  vision: boolean;
  ctx: number | null;
  gpu_layers: number | null;
  pid: number | null;
  active_streams: number;
  runtime: string | null;
  vram_estimate_mb: number | null;
}

export interface ChatModel {
  ref: string;
  name: string;
  provider: string;
  vision: boolean;
  context_max: number | null;
  default_ctx?: number | null;
  vram_mb?: number | null;
  size?: number | null;
  local: boolean;
  /** Server profile id hosting the model (set by the desktop). */
  engine?: string;
}

export interface Attachment {
  id: ID;
  name: string | null;
  mime: string;
  size: number;
  url: string;
  width?: number | null;
  height?: number | null;
}

export interface SearchSource {
  n: number;
  title: string;
  url: string;
  snippet: string;
  domain: string;
  fetched: boolean;
}

export interface SearchInfo {
  query: string;
  provider?: string;
  error?: string | null;
  sources: SearchSource[];
}

export interface MessageStats {
  search?: SearchInfo;
  elapsed_s?: number;
  ttft_s?: number;
  tokens_per_s?: number;
  completion_tokens?: number;
  prompt_tokens?: number;
  finish_reason?: string;
  error?: string;
}

export interface Message {
  id: ID;
  conversation_id: ID;
  parent_id: ID | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  reasoning?: string | null;
  attachments: Attachment[];
  model_ref: string | null;
  stats: MessageStats | null;
  status: 'complete' | 'partial' | 'stopped' | 'error';
  created_at: number;
  /** Live web-search state while this reply is being generated (the stored copy is `stats.search`). */
  search?: SearchInfo;
}

export interface ChatParams {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  min_p?: number;
  repeat_penalty?: number;
  max_tokens?: number;
  seed?: number;
  ctx?: number;
  gpu_layers?: number;
  thinking?: boolean;
}

export interface Conversation {
  id: ID;
  title: string;
  project_id: ID | null;
  folder_id: ID | null;
  pinned: number;
  model_ref: string | null;
  system_prompt: string;
  params: ChatParams;
  active_leaf_id: ID | null;
  created_at: number;
  updated_at: number;
  message_count?: number;
  messages?: Message[];
}

export type ChatEvent =
  | { type: 'user_message'; message: Message }
  | { type: 'title'; title: string }
  | { type: 'assistant_start'; message: Message }
  | { type: 'status'; text: string }
  | { type: 'delta'; content: string; reasoning: string }
  | { type: 'search'; phase: 'started' | 'results' | 'unavailable'; query: string; provider?: string; error?: string | null; sources?: SearchSource[] }
  | { type: 'done'; message: Message }
  | { type: 'error'; message: string };

export interface EngineSettings {
  worker_idle_unload_minutes: number;
  llm_idle_unload_minutes: number;
  auto_unload_llm_for_generation: boolean;
  vram_headroom_mb: number;
  extra_model_dirs: string[];
  llm_endpoints: { id: string; name: string; base_url: string; api_key: boolean | string; models?: string[]; vision?: boolean }[];
  llm_default_ctx: number;
  llm_gpu_layers: number;
  llm_flash_attn: boolean;
  hf_token: boolean | string;
  gateway_url: string;
  gateway_token: boolean | string;
  oom_retry_with_offload: boolean;
  job_max_attempts: number;
  search_provider: 'auto' | 'searxng' | 'duckduckgo' | 'brave';
  search_searxng_url: string;
  search_brave_key: boolean | string;
  search_policy: 'manual' | 'always';
  search_max_results: number;
  search_fetch_pages: number;
  search_timeout_s: number;
}

export interface EngineEvent<T = any> {
  type: string;
  ts?: number;
  data: T;
  /** Server profile id of the engine that sent the event (set by the desktop). */
  engine?: string;
}

/** A model as an engine advertises it in its capabilities document. */
export interface CapabilityModel {
  id: string;
  name: string;
  category: ModelCategory | null;
  family: string | null;
  runtime: string;
  /** Workloads: chat, code, vision, reasoning, image, video, upscale. */
  kinds: string[];
  modes: string[];
  quantization: string | null;
  size_bytes: number | null;
  memory_mb: number | null;
  supported: boolean;
  loaded: boolean;
  chat_ref: string | null;
}

/** `GET /v1/capabilities`: identity, hardware, models and load of one engine. */
export interface Capabilities {
  server_id: string;
  engine_version: string;
  api_version: string;
  hostname: string;
  arch: string;
  os: string;
  cpu: string;
  gpu: {
    name: string;
    compute_capability: string | null;
    unified_memory: boolean;
    memory_total_mb: number;
    memory_free_mb: number;
    utilization_pct: number | null;
    driver: string | null;
    cuda: string | null;
  } | null;
  memory: { total_mb: number; used_mb: number; available_mb: number };
  storage_free_gb: number;
  backends: string[];
  experiments: string[];
  chat_endpoints: { id: string; name: string }[];
  models: CapabilityModel[];
  llm: { state: string; model_id: string | null; active_streams: number };
  jobs: { running: number; queued: number; concurrency: number; queue_soft_limit: number; busy_reason: string | null };
  features: string[];
  health: { status: 'ok' | 'degraded'; issues: string[] };
  generated_at: number;
  /** Built by the desktop from older endpoints (engine without `/v1/capabilities`). */
  derived?: boolean;
}
