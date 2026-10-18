import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import Reasoning from './Reasoning.svelte';

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
  document.body.innerHTML = '';
});

function render(init: { reasoning: string; streaming: boolean; answering: boolean }) {
  const state = $state({ ...init });
  const target = document.body.appendChild(document.createElement('div'));
  const comp = mount(Reasoning, {
    target,
    props: { get reasoning() { return state.reasoning; }, get streaming() { return state.streaming; }, get answering() { return state.answering; } },
  });
  cleanup = () => unmount(comp);
  flushSync();
  return { state, target, text: () => target.textContent?.replace(/\s+/g, ' ').trim() ?? '', head: () => target.querySelector<HTMLButtonElement>('button.head') };
}

describe('live reasoning line', () => {
  it('shows one latest line while reasoning streams, without expanding anything', () => {
    const r = render({ reasoning: 'Checking the available context. Comparing the possible approaches', streaming: true, answering: false });
    expect(r.text()).toBe('Comparing the possible approaches');
    expect(r.target.querySelector('#trace')).toBeNull();
    r.state.reasoning += ' and their costs';
    flushSync();
    expect(r.text()).toBe('Comparing the possible approaches and their costs');
  });

  it('expands to the full chronological trace on click and updates live', () => {
    const r = render({ reasoning: 'One thought. Two thought', streaming: true, answering: false });
    r.head()!.click();
    flushSync();
    expect(r.target.querySelector('#trace')?.textContent).toBe('One thought. Two thought');
    r.state.reasoning += '. Three thought';
    flushSync();
    expect(r.target.querySelector('#trace')?.textContent).toBe('One thought. Two thought. Three thought');
    r.head()!.click(); // collapsing never touches generation
    flushSync();
    expect(r.target.querySelector('#trace')).toBeNull();
  });

  it('collapses on its own when the answer starts, and offers "See thinking"', () => {
    const r = render({ reasoning: 'Plan the reply.', streaming: true, answering: false });
    expect(r.head()?.getAttribute('aria-expanded')).toBe('false');
    r.state.answering = true;
    flushSync();
    expect(r.text()).toBe('See thinking');
  });

  it('stays open after finishing if the reader opened it deliberately', () => {
    const r = render({ reasoning: 'Plan the reply.', streaming: true, answering: false });
    r.head()!.click();
    flushSync();
    r.state.answering = true;
    r.state.streaming = false;
    flushSync();
    expect(r.target.querySelector('#trace')?.textContent).toBe('Plan the reply.');
    expect(r.text()).toContain('Hide thinking');
  });

  it('persisted messages show a collapsed "See thinking" that expands on demand', () => {
    const r = render({ reasoning: 'Old trace', streaming: false, answering: true });
    expect(r.text()).toBe('See thinking');
    r.head()!.click();
    flushSync();
    expect(r.target.querySelector('#trace')?.textContent).toBe('Old trace');
  });

  it('shows only a truthful activity indicator for models that expose no reasoning', () => {
    const r = render({ reasoning: '', streaming: true, answering: false });
    expect(r.text()).toBe('Generating…');
    r.state.answering = true;
    flushSync();
    expect(r.text()).toBe('');
    const done = render({ reasoning: '', streaming: false, answering: true });
    expect(done.text()).toBe('');
  });
});
