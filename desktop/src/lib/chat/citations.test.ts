import { describe, expect, it } from 'vitest';
import { linkCitations } from './citations';

const src = [
  { n: 1, title: 'Rust "Book"', url: 'https://doc.rust-lang.org/book/', snippet: '', domain: 'doc.rust-lang.org', fetched: true },
  { n: 2, title: 'Evil', url: 'javascript:alert(1)', snippet: '', domain: '', fetched: false },
];

describe('linkCitations', () => {
  it('links only numbers that name a real, http(s) source', () => {
    const out = linkCitations('<p>Owned values [1] and invented [7] and unsafe [2].</p>', src);
    expect(out).toContain('<a class="cite" href="https://doc.rust-lang.org/book/"');
    expect(out).toContain('title="Rust &quot;Book&quot;"');
    expect(out).toContain('invented [7]');
    expect(out).toContain('unsafe [2]');
    expect(out.match(/class="cite"/g)).toHaveLength(1);
  });
  it('leaves code, preformatted text and existing links untouched', () => {
    const html = '<pre><code>a[1]</code></pre><p><code>b[1]</code> <a href="x">c[1]</a> d[1]</p>';
    const out = linkCitations(html, src);
    expect(out.match(/class="cite"/g)).toHaveLength(1);
    expect(out).toContain('<code>a[1]</code>');
    expect(out).toContain('<a href="x">c[1]</a>');
  });
  it('is a no-op without sources', () => {
    expect(linkCitations('<p>x [1]</p>', undefined)).toBe('<p>x [1]</p>');
    expect(linkCitations('<p>x [1]</p>', [])).toBe('<p>x [1]</p>');
  });
});
