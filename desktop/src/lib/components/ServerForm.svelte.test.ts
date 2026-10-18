import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const invokeMock = vi.fn();
// Bypasses the spy for rejections: the spy's own result tracking would report a rejected promise as unhandled.
let rawInvoke: ((...a: unknown[]) => unknown) | null = null;
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...a: unknown[]) => (rawInvoke ? rawInvoke(...a) : invokeMock(...a)) }));
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(async () => () => {}) }));

import ServerForm from './ServerForm.svelte';
import type { Profile } from '../stores/connection.svelte';

const PIN = 'sha256:' + 'ab'.repeat(32);
const OTHER = 'sha256:' + 'cd'.repeat(32);

let cleanup: (() => void) | null = null;
beforeEach(() => {
  invokeMock.mockReset();
  rawInvoke = null;
});
afterEach(() => {
  cleanup?.();
  cleanup = null;
  document.body.innerHTML = '';
});

function render(profile: Profile | null = null) {
  const target = document.body.appendChild(document.createElement('div'));
  const comp = mount(ServerForm, { target, props: { profile } });
  cleanup = () => unmount(comp);
  flushSync();
  return target;
}

const labels = (t: HTMLElement) => Array.from(t.querySelectorAll('.field .label')).map((l) => l.textContent?.trim());
const radios = (t: HTMLElement) => Array.from(t.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
const button = (t: HTMLElement, text: string) => Array.from(t.querySelectorAll('button')).find((b) => b.textContent?.includes(text)) as HTMLButtonElement | undefined;
const input = (t: HTMLElement, placeholder: string) => t.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
function type(el: HTMLInputElement, value: string) {
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
const settle = () => vi.waitFor(() => flushSync());

const saved = (over: Partial<Profile> = {}): Profile => ({
  id: 'p1', name: 'Box', host: 'box.example', port: 8742, scheme: 'http', mode: 'direct', ssh: null, has_token: true,
  transport: 'snp', snp_port: 8744, snp_pin: PIN, snp_server_name: null, ...over,
});

describe('server form', () => {
  it('offers SNP, HTTP and HTTPS as the protocol and an independent SSH tunnel toggle', () => {
    const t = render();
    expect(radios(t).map((r) => r.textContent?.trim())).toEqual(['SNP', 'HTTP', 'HTTPS']);
    expect(radios(t)[0].getAttribute('aria-checked')).toBe('true');
    expect(t.textContent).toContain('SNP - native protocol');
    expect(t.textContent).toContain('Connect through SSH tunnel');
    expect(t.querySelector('input[placeholder="ubuntu"]')).toBeNull(); // tunnel fields hidden until enabled
  });

  it('shows the SNP port and server identity for SNP, and the API port for HTTP/HTTPS', () => {
    const t = render();
    expect(labels(t)).toContain('SNP port');
    expect(labels(t)).toContain('Server identity (pin)');
    expect(labels(t)).not.toContain('API port');
    radios(t)[1].click();
    flushSync();
    expect(labels(t)).toContain('API port');
    expect(labels(t)).not.toContain('SNP port');
    expect(labels(t)).not.toContain('Server identity (pin)');
    expect(t.textContent).toContain('HTTP - direct');
    radios(t)[2].click();
    flushSync();
    expect(t.textContent).toContain('HTTPS - encrypted HTTP');
  });

  it('keeps tunnelling independent of the protocol', () => {
    const t = render();
    t.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    flushSync();
    expect(t.querySelector('input[placeholder="ubuntu"]')).not.toBeNull();
    expect(radios(t)[0].getAttribute('aria-checked')).toBe('true'); // still SNP
    expect(labels(t)).toContain('SNP port');
    expect(labels(t)).toContain('Engine address on the server');
    radios(t)[1].click();
    flushSync();
    expect(t.querySelector('input[placeholder="ubuntu"]')).not.toBeNull(); // still tunnelled
    expect(labels(t)).toContain('Engine port');
  });

  it('fetches the identity but only stores it after explicit confirmation', async () => {
    invokeMock.mockResolvedValue({ pin: PIN, fingerprint: 'AB:AB' });
    const t = render();
    type(input(t, '192.168.1.50'), 'box.example');
    button(t, 'Fetch identity')!.click();
    await vi.waitFor(() => expect(t.querySelector('.confirm')).not.toBeNull());
    expect(invokeMock).toHaveBeenCalledTimes(1);
    const [cmd, args] = invokeMock.mock.calls[0];
    expect(cmd).toBe('snp_probe_pin');
    expect(args.profile).toMatchObject({ host: 'box.example', transport: 'snp', snp_pin: null, snp_port: 8744 });
    // The fingerprint is shown for comparison; the pin field is still empty.
    expect(t.querySelector('.fp')!.textContent).toBe('AB:'.repeat(31) + 'AB');
    expect(input(t, 'sha256:…').value).toBe('');
    button(t, 'Cancel')!.click();
    flushSync();
    expect(input(t, 'sha256:…').value).toBe('');
    // Fetch again and trust.
    button(t, 'Fetch identity')!.click();
    await vi.waitFor(() => expect(t.querySelector('.confirm')).not.toBeNull());
    button(t, 'Trust this identity')!.click();
    flushSync();
    expect(input(t, 'sha256:…').value).toBe(PIN);
    expect(t.querySelector('.confirm')).toBeNull();
  });

  it('warns loudly when the server presents a different identity than the saved one', async () => {
    invokeMock.mockResolvedValue({ pin: OTHER, fingerprint: 'CD:CD' });
    const t = render(saved());
    button(t, 'Fetch identity')!.click();
    await vi.waitFor(() => expect(t.querySelector('.confirm.changed')).not.toBeNull());
    expect(t.querySelector('.confirm')!.textContent).toContain('different identity');
    expect(t.querySelector('.confirm')!.textContent).toContain(PIN);
    expect(input(t, 'sha256:…').value).toBe(PIN); // unchanged until confirmed
  });

  it('shows why a probe failed', async () => {
    rawInvoke = async () => {
      throw 'Connection to 127.0.0.1:8744 was refused.';
    };
    const t = render();
    type(input(t, '192.168.1.50'), '127.0.0.1');
    button(t, 'Fetch identity')!.click();
    await vi.waitFor(() => expect(t.textContent).toContain('was refused'));
  });

  it('reports negotiated SNP details after a successful test', async () => {
    invokeMock.mockResolvedValue({
      ok: true, stage: 'ok', message: 'Connected over SNP', latency_ms: 4, transport: 'snp', system: null,
      health: { engine_version: '0.4.0', api_version: '1.5' },
      snp: {
        version: 1, server: 'sarness-engine/0.4.0', features: ['events', 'files'], api_version: '1.5', pin: PIN, fingerprint: 'AB:AB', handshake_ms: 3,
        limits: { max_frame: 1048576, initial_window: 262144, conn_window: 4194304, max_streams: 64 },
      },
    });
    const t = render(saved());
    button(t, 'Test connection')!.click();
    await vi.waitFor(() => expect(t.textContent).toContain('Connected over SNP'));
    expect(t.textContent).toContain('SNP v1');
    expect(t.textContent).toContain('handshake 3 ms');
    expect(t.textContent).toContain('events');
    expect(t.textContent).toContain('frames up to 1 MiB');
    expect(invokeMock.mock.calls[0][0]).toBe('test_connection');
  });

  it('shows both pins on a pin mismatch and only offers to review, never to auto-accept', async () => {
    invokeMock.mockResolvedValue({
      ok: false, stage: 'identity', transport: 'snp', error_kind: 'pin_mismatch', latency_ms: null, health: null, system: null,
      message: `The server's identity does not match the saved one. Expected ${PIN} but the server presented ${OTHER}.`,
      expected_pin: PIN, presented_pin: OTHER,
    });
    const t = render(saved());
    button(t, 'Test connection')!.click();
    await vi.waitFor(() => expect(t.querySelector('.pins')).not.toBeNull());
    expect(t.querySelector('.pins')!.textContent).toContain(PIN);
    expect(t.querySelector('.pins')!.textContent).toContain(OTHER);
    expect(input(t, 'sha256:…').value).toBe(PIN);
    button(t, 'Review the new identity')!.click();
    flushSync();
    expect(t.querySelector('.confirm.changed')).not.toBeNull();
    expect(input(t, 'sha256:…').value).toBe(PIN); // still the old pin until the user confirms
  });

  it('blocks an invalid pin before it reaches the backend', async () => {
    const t = render(saved({ snp_pin: 'sha256:xyz' }));
    await settle();
    expect(t.textContent).toContain('64 hexadecimal digits');
    expect(button(t, 'Test connection')!.disabled).toBe(true);
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it('does not advertise Tailscale in the default view', () => {
    const t = render();
    expect(t.textContent!.toLowerCase()).not.toContain('tailscale');
    expect(t.textContent!.toLowerCase()).not.toContain('tailnet');
    expect(t.querySelector('.discover')!.textContent).toContain('Find servers');
  });
});
