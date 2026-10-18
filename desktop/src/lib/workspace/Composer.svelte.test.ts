import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Composer from './Composer.svelte';
import type { Mode } from './params';

let cleanup: (() => void) | null = null;
afterEach(() => {
  cleanup?.();
  cleanup = null;
  document.body.innerHTML = '';
});

function render(over: Record<string, unknown> = {}) {
  const calls = { mode: vi.fn(), send: vi.fn(), stop: vi.fn(), attach: vi.fn(), remove: vi.fn(), model: vi.fn() };
  const state = $state({ mode: 'chat' as Mode, text: '', settingsOpen: false, streaming: false });
  const target = document.body.appendChild(document.createElement('div'));
  const props: Record<string, unknown> = {
    get mode() { return state.mode; },
    get text() { return state.text; },
    set text(v: string) { state.text = v; },
    get settingsOpen() { return state.settingsOpen; },
    set settingsOpen(v: boolean) { state.settingsOpen = v; },
    get streaming() { return state.streaming; },
    placeholder: 'Message', modelOptions: [{ value: 'a', label: 'Model A' }], model: 'a', onmodel: calls.model,
    chips: ['1:1', '8 steps'], attachments: [], accept: 'image/*', attachHint: 'Attach', 
    onmode: (m: Mode) => { calls.mode(m); state.mode = m; }, onsend: calls.send, onstop: calls.stop, onattach: calls.attach, onremove: calls.remove,
    settings: (anchor: Comment) => { const d = document.createElement('div'); d.id = 'settings-body'; d.textContent = 'settings'; anchor.parentNode?.insertBefore(d, anchor); },
    ...over,
  };
  const comp = mount(Composer, { target, props: props as any });
  cleanup = () => unmount(comp);
  flushSync();
  return { calls, state, target };
}
const radios = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>('[role="radio"]'));

describe('universal composer', () => {
  it('has one prompt box and exactly the four modes, in order, inside it', () => {
    const { target } = render();
    expect(target.querySelectorAll('textarea')).toHaveLength(1);
    expect(radios(target).map((r) => r.textContent?.trim())).toEqual(['Chat', 'Code', 'Image', 'Video']);
    expect(target.querySelector('.composer')!.contains(radios(target)[0])).toBe(true); // mode picker lives IN the composer
  });

  it('marks the active mode and switches by click without remounting the prompt box', () => {
    const { target, calls, state } = render();
    const box = target.querySelector('textarea')!;
    expect(radios(target)[0].getAttribute('aria-checked')).toBe('true');
    radios(target)[2].click();
    flushSync();
    expect(calls.mode).toHaveBeenCalledWith('image');
    expect(radios(target).map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true', 'false']);
    expect(target.querySelector('textarea')).toBe(box); // same element: context and typed text survive
    expect(state.mode).toBe('image');
  });

  it('keeps typed text across mode changes', () => {
    const { target, state } = render();
    const box = target.querySelector('textarea')!;
    box.value = 'a lighthouse at dusk';
    box.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    radios(target)[3].click();
    flushSync();
    expect(state.text).toBe('a lighthouse at dusk');
    expect(target.querySelector('textarea')!.value).toBe('a lighthouse at dusk');
  });

  it('is keyboard operable: arrow keys move between modes, roving tabindex', () => {
    const { target, calls } = render();
    const group = target.querySelector('[role="radiogroup"]')!;
    expect(radios(target).map((r) => r.tabIndex)).toEqual([0, -1, -1, -1]);
    radios(target)[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    flushSync();
    expect(calls.mode).toHaveBeenLastCalledWith('code');
    group.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    group.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    flushSync();
    expect(calls.mode).toHaveBeenLastCalledWith('video'); // wraps around
  });

  it('Enter sends, Shift+Enter does not', () => {
    const { target, calls } = render();
    const box = target.querySelector('textarea')!;
    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true }));
    expect(calls.send).not.toHaveBeenCalled();
    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    expect(calls.send).toHaveBeenCalledTimes(1);
  });

  it('send is disabled when empty; becomes a stop button while streaming (composer stays usable)', () => {
    const { target, state, calls } = render();
    expect(target.querySelector<HTMLButtonElement>('button[aria-label="Send"]')!.disabled).toBe(true);
    state.streaming = true;
    flushSync();
    const stop = target.querySelector<HTMLButtonElement>('button[aria-label="Stop generating"]')!;
    expect(stop).toBeTruthy();
    stop.click();
    expect(calls.stop).toHaveBeenCalled();
    expect(target.querySelector('textarea')!.disabled).toBe(false); // still typeable during generation
  });

  it('opens a settings region named after the active mode, and mode changes update it', () => {
    const { target, state } = render();
    const btn = target.querySelector<HTMLButtonElement>('.settingsbtn')!;
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(target.querySelector('[role="region"]')).toBeNull();
    btn.click();
    flushSync();
    expect(target.querySelector('[role="region"]')!.getAttribute('aria-label')).toBe('Chat settings');
    radios(target)[2].click();
    flushSync();
    expect(btn.textContent).toContain('Image settings');
    expect(target.querySelector('[role="region"]')!.getAttribute('aria-label')).toBe('Image settings');
    expect(state.settingsOpen).toBe(true); // switching modes does not close the panel
  });

  it('the attach control is a real, keyboard-focusable button', () => {
    const { target } = render();
    const btn = target.querySelector<HTMLButtonElement>('button.iconbtn')!;
    expect(btn.tagName).toBe('BUTTON');
    expect(btn.tabIndex).toBe(0);
    expect(btn.getAttribute('aria-label')).toMatch(/^Attach/);
  });

  it('shows the summary chips and attachments with a remove control', () => {
    const { target, calls } = render({ attachments: [{ id: 'f1', name: 'ref.png', url: 'x', image: true }] });
    expect(Array.from(target.querySelectorAll('.chip')).map((c) => c.textContent)).toEqual(['1:1', '8 steps']);
    target.querySelector<HTMLButtonElement>('.pchip button')!.click();
    expect(calls.remove).toHaveBeenCalledWith('f1');
  });
});
