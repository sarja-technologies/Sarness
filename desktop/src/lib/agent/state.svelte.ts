import { emptyAgentState, hydrateAgent, serializeAgent, type AgentSessionState } from './persist';
import type { Project } from './native';

const KEY = 'sarness.agent.v1';

function load(): Record<string, AgentSessionState> {
  try {
    return hydrateAgent(JSON.parse(localStorage.getItem(KEY) ?? '{}'));
  } catch {
    return {};
  }
}

export const agentStore = $state<{ sessions: Record<string, AgentSessionState>; projects: Project[] }>({ sessions: load(), projects: [] });

let timer: ReturnType<typeof setTimeout> | null = null;
$effect.root(() => {
  $effect(() => {
    // Deep-read through a snapshot so every nested change (items, tool output) schedules a save.
    const snap = $state.snapshot(agentStore.sessions);
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      try {
        localStorage.setItem(KEY, serializeAgent(snap));
      } catch {
        /* storage full or unavailable: the live state still works */
      }
    }, 400);
  });
});

export function agentFor(sessionId: string): AgentSessionState {
  if (!agentStore.sessions[sessionId]) agentStore.sessions[sessionId] = emptyAgentState();
  return agentStore.sessions[sessionId];
}

export function dropAgent(sessionId: string) {
  delete agentStore.sessions[sessionId];
}
