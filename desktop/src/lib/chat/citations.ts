import type { SearchSource } from '../api/types';

const SAFE = /^https?:\/\//i;

function esc(s: string) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Turns `[n]` into a link to source n. Only numbers that name a real source become links, so the model
 * cannot cite something the search did not return; code, preformatted text and existing links are left alone. */
export function linkCitations(html: string, sources: SearchSource[] | undefined): string {
  if (!sources?.length || !html.includes('[')) return html;
  const byN = new Map(sources.filter((s) => SAFE.test(s.url)).map((s) => [s.n, s]));
  if (!byN.size) return html;
  let skip = 0;
  return html
    .split(/(<[^>]+>)/)
    .map((part) => {
      if (part.startsWith('<')) {
        const m = /^<(\/?)(pre|code|a)\b/i.exec(part);
        if (m) skip = Math.max(0, skip + (m[1] ? -1 : 1));
        return part;
      }
      if (skip) return part;
      return part.replace(/\[(\d{1,2})\]/g, (whole, d) => {
        const s = byN.get(Number(d));
        return s ? `<a class="cite" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" title="${esc(s.title)}">${d}</a>` : whole;
      });
    })
    .join('');
}
