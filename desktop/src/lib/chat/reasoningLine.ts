/** Picks the single line shown while a model is reasoning. Only the tail of the trace is examined, so
 * a very long trace costs the same as a short one on every streamed token. */

const TAIL = 900;
const MIN_PARTIAL = 18;

export interface LiveLine {
  text: string;
  /** Absolute offset where the shown segment starts: stable while the segment grows, changes when a new one begins. */
  key: number;
}

function clean(s: string): string {
  return s
    .replace(/^[\s>*#\-•\d.)]+/, '')
    .replace(/[*_`]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

export function latestReasoningLine(reasoning: string): LiveLine | null {
  if (!reasoning.trim()) return null;
  const base = Math.max(0, reasoning.length - TAIL);
  const tail = reasoning.slice(base);
  // Segment boundaries: line breaks and sentence ends. The final segment is usually unfinished.
  const parts: { start: number; text: string }[] = [];
  const re = /[^\n.!?…:]+(?:[.!?…:]+|\n+|$)/g;
  for (let m = re.exec(tail); m && m[0]; m = re.exec(tail)) {
    const text = clean(m[0]);
    if (text) parts.push({ start: base + m.index, text });
  }
  if (!parts.length) return null;
  const last = parts[parts.length - 1];
  // Show the live fragment once it is long enough to read; until then keep the last finished thought.
  const pick = last.text.length >= MIN_PARTIAL || parts.length === 1 ? last : parts[parts.length - 2];
  return { text: pick.text, key: pick.start };
}
