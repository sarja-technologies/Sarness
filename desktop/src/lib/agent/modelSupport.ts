// Honest guidance about whether a chat model can drive tools. We only know names, so this is a
// hint; the loop additionally detects a model that writes tool calls as text.
export type ToolSupport = 'likely' | 'unknown' | 'unlikely';

const LIKELY = /(qwen-?[23]|qwq|llama-?3\.[1-9]|llama-?4|mistral|mixtral|ministral|devstral|hermes|functionary|command-?r|glm-?4|gpt-?oss|granite|firefunction|deepseek-?(v3|r1)|kimi|minimax|nemotron)/i;
const UNLIKELY = /(\b0\.5b|\b1b\b|\b1\.5b|tiny|phi-?2|gemma-?2|embed|rerank)/i;

export function toolSupport(name: string | undefined | null): ToolSupport {
  if (!name) return 'unknown';
  if (UNLIKELY.test(name)) return 'unlikely';
  return LIKELY.test(name) ? 'likely' : 'unknown';
}

export function supportNote(s: ToolSupport): string | null {
  if (s === 'likely') return null;
  if (s === 'unlikely') return 'This model is too small or old to drive tools reliably. Choose a larger instruction-tuned model.';
  return 'Tool use is not verified for this model. If it writes commands as text instead of calling tools, choose a model such as Qwen or Llama 3.1+.';
}
