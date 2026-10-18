// Types shared by the agent loop, store and UI. UI-free.

export type PermissionMode = 'ask' | 'workspace_auto' | 'full_access';
export type Risk = 'low' | 'medium' | 'high' | 'critical';

export const PERMISSION_LABEL: Record<PermissionMode, string> = {
  ask: 'Ask',
  workspace_auto: 'Workspace Auto',
  full_access: 'Full Access',
};
export const PERMISSION_HINT: Record<PermissionMode, string> = {
  ask: 'Confirm every file change and command.',
  workspace_auto: 'Edit files and run ordinary commands inside the project. Ask for installs, network, anything outside, or risky actions.',
  full_access: 'Run without routine prompts, including installs and paths outside the project. Destructive, remote and privileged actions still ask.',
};

export type ChatToolCall = { id: string; type: 'function'; function: { name: string; arguments: string } };
export type AgentMessage =
  | { role: 'system'; content: string }
  | { role: 'user'; content: string }
  | { role: 'assistant'; content: string; tool_calls?: ChatToolCall[] }
  | { role: 'tool'; tool_call_id: string; content: string };

export interface ToolRequest {
  workspace: string;
  run_id: string;
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
  mode: PermissionMode;
  approval_id?: string | null;
}

export type ToolResponse =
  | { status: 'ok'; content: string; ui: any }
  | { status: 'error'; content: string; ui: any }
  | { status: 'needs_approval'; approval_id: string; risk: Risk; title: string; reason: string; preview: any }
  | { status: 'denied'; content: string };

export interface ModelTurn {
  content: string;
  reasoning: string;
  toolCalls: ChatToolCall[];
  finish: string | null;
}

export type Phase = 'Thinking' | 'Reading' | 'Editing' | 'Running' | 'Testing' | 'Waiting for approval' | 'Completed' | 'Stopped' | 'Blocked' | 'Failed';

export interface ApprovalInfo {
  approvalId: string;
  risk: Risk;
  title: string;
  reason: string;
  preview: any;
  state: 'waiting' | 'approved' | 'denied';
}

export type ToolStatus = 'running' | 'awaiting' | 'done' | 'failed' | 'denied' | 'cancelled';

export type AgentItem =
  | { kind: 'text'; id: string; text: string; reasoning?: string; final?: boolean }
  | { kind: 'tool'; id: string; callId: string; name: string; args: Record<string, unknown>; status: ToolStatus; content?: string; ui?: any; live?: string; approval?: ApprovalInfo; startedAt: number; endedAt?: number }
  | { kind: 'plan'; id: string; steps: { text: string; status: 'pending' | 'in_progress' | 'done' }[] }
  | { kind: 'notice'; id: string; level: 'info' | 'warn' | 'error'; text: string };

export type RunStatus = 'running' | 'completed' | 'cancelled' | 'blocked' | 'error';

export interface AgentRun {
  id: string;
  prompt: string;
  created: number; // unix seconds
  ended?: number;
  status: RunStatus;
  reason?: string; // why blocked / failed
  model: string | null;
  permission: PermissionMode;
  items: AgentItem[];
  disk?: import('./snapshot').DiskChanges; // what really changed on disk during the task
  phase?: Phase; // live status while running
  drafting?: string; // name of the tool call the model is currently writing
}

export type LoopEvent =
  | { type: 'phase'; phase: Phase }
  | { type: 'drafting'; name: string | null }
  | { type: 'text'; id: string; text: string; reasoning: string; final?: boolean } // cumulative content of one assistant turn
  | { type: 'tool_start'; id: string; callId: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_approval'; callId: string; approval: ApprovalInfo }
  | { type: 'tool_approval_result'; callId: string; approved: boolean }
  | { type: 'tool_result'; callId: string; status: ToolStatus; content: string; ui: any }
  | { type: 'plan'; steps: { text: string; status: 'pending' | 'in_progress' | 'done' }[] }
  | { type: 'notice'; level: 'info' | 'warn' | 'error'; text: string };

export type RunOutcome =
  | { status: 'completed'; final: string }
  | { status: 'cancelled' }
  | { status: 'blocked'; reason: string }
  | { status: 'error'; reason: string };
