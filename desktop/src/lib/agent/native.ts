// Bridge to the native (Rust) local-agent layer. Everything here runs on the machine that runs
// the desktop app. In a plain browser (dev server) there is no native layer, and Code mode says so.
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { isTauri } from '../api/transport';
import type { PermissionMode, ToolRequest, ToolResponse } from './types';

export interface Project {
  path: string;
  name: string;
  last_opened: number;
}
export interface TreeNode {
  name: string;
  dir: boolean;
  children?: TreeNode[];
}
export interface GitState {
  repo: boolean;
  branch?: string;
  files?: { status: string; path: string }[];
}
export interface OutputChunk {
  run_id: string;
  call_id: string;
  stream: 'stdout' | 'stderr';
  text: string;
}

export interface NativeAgent {
  available: boolean;
  toolDefs(): Promise<any[]>;
  call(req: ToolRequest): Promise<ToolResponse>;
  cancel(runId: string): Promise<void>;
  endRun(runId: string): Promise<number>;
  resetRun(runId: string): Promise<void>;
  projects(): Promise<Project[]>;
  addProject(path: string): Promise<Project[]>;
  removeProject(path: string): Promise<Project[]>;
  createProject(parent: string, name: string): Promise<string>;
  tree(path: string, depth?: number): Promise<TreeNode>;
  git(path: string): Promise<GitState>;
  diff(path: string, file?: string): Promise<string>;
  snapshot(path: string): Promise<{ files: Record<string, [number, number]>; truncated: boolean }>;
  pickFolder(title: string): Promise<string | null>;
  onOutput(cb: (c: OutputChunk) => void): Promise<() => void>;
}

const tauri = async () => ({ invoke, listen, open: openDialog });

const unavailable = () => new Error('The local coding agent runs in the Sarness desktop app. Open this project in the desktop app to use Code mode.');

export const native: NativeAgent = {
  available: isTauri,
  async toolDefs() {
    return (await tauri()).invoke('agent_tool_defs');
  },
  async call(req) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('agent_call', { req });
  },
  async cancel(runId) {
    if (isTauri) await (await tauri()).invoke('agent_cancel', { runId });
  },
  async endRun(runId) {
    return isTauri ? (await tauri()).invoke('agent_end_run', { runId }) : 0;
  },
  async resetRun(runId) {
    if (isTauri) await (await tauri()).invoke('agent_reset_run', { runId });
  },
  async projects() {
    return isTauri ? (await tauri()).invoke('projects_list') : [];
  },
  async addProject(path) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('projects_add', { path });
  },
  async removeProject(path) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('projects_remove', { path });
  },
  async createProject(parent, name) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('project_create', { parent, name });
  },
  async tree(path, depth = 3) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('project_tree', { path, depth });
  },
  async git(path) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('project_git', { path });
  },
  async diff(path, file) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('project_diff', { path, file: file ?? null });
  },
  async snapshot(path) {
    if (!isTauri) throw unavailable();
    return (await tauri()).invoke('project_snapshot', { path });
  },
  async pickFolder(title) {
    if (!isTauri) throw unavailable();
    const t = await tauri();
    const r = await t.open({ directory: true, multiple: false, title });
    return typeof r === 'string' ? r : null;
  },
  async onOutput(cb) {
    if (!isTauri) return () => {};
    const t = await tauri();
    return t.listen<OutputChunk>('agent://output', (e) => cb(e.payload));
  },
};

export type { PermissionMode };
