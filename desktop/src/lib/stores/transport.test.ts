import { describe, expect, it } from 'vitest';
import {
  activePort, describeLimits, describeSession, endpointLabel, isValidPin, normalizePin, pinFingerprint, profileTransport,
  shortPin, transportLabel, validateForm,
} from './transport';

const PIN = 'sha256:' + 'ab'.repeat(32);

describe('transport helpers', () => {
  it('derives the transport of old profiles from their scheme', () => {
    expect(profileTransport({ scheme: 'http' })).toBe('http');
    expect(profileTransport({ scheme: 'https' })).toBe('https');
    expect(profileTransport({ transport: 'snp', scheme: 'http' })).toBe('snp');
  });

  it('picks the port of the active transport', () => {
    expect(activePort({ transport: 'snp', scheme: 'http', port: 8742, snp_port: 9000 })).toBe(9000);
    expect(activePort({ transport: 'snp', scheme: 'http', port: 8742 })).toBe(8744);
    expect(activePort({ transport: 'http', scheme: 'http', port: 8742, snp_port: 9000 })).toBe(8742);
  });

  it('labels endpoints independently of tunnelling', () => {
    expect(endpointLabel({ transport: 'snp', host: 'box.example', port: 8742, snp_port: 8744, mode: 'direct' })).toBe('snp://box.example:8744');
    expect(endpointLabel({ transport: 'https', host: 'fd00::1', port: 8443, mode: 'direct' })).toBe('https://[fd00::1]:8443');
    expect(endpointLabel({ host: '127.0.0.1', port: 8742, scheme: 'http', mode: 'ssh', ssh: { user: 'me', host: 'box' } })).toBe('ssh me@box → http://127.0.0.1:8742');
    expect(endpointLabel({ transport: 'snp', host: '127.0.0.1', port: 8742, mode: 'ssh', ssh: { user: 'me', host: 'box' } })).toBe('ssh me@box → snp://127.0.0.1:8744');
  });

  it('validates and formats pins', () => {
    expect(isValidPin(PIN)).toBe(true);
    expect(isValidPin(`  ${PIN.toUpperCase().replace('SHA256', 'sha256')}  `)).toBe(true);
    expect(isValidPin('sha256:abcd')).toBe(false);
    expect(isValidPin('md5:' + 'ab'.repeat(32))).toBe(false);
    expect(normalizePin(' SHA256:AB ')).toBe('sha256:ab');
    expect(pinFingerprint(PIN).startsWith('AB:AB:AB')).toBe(true);
    expect(pinFingerprint(PIN).split(':')).toHaveLength(32);
    expect(shortPin(PIN)).toMatch(/^sha256:abababab…ababab$/);
  });

  it('describes a negotiated session without secrets', () => {
    const s = {
      version: 1, server: 'sarness-engine/0.4.0', features: ['events', 'files'], api_version: '1.4', pin: PIN, handshake_ms: 3,
      limits: { max_frame: 1048576, initial_window: 262144, conn_window: 4194304, max_streams: 64 },
    };
    expect(describeSession(s)).toBe('SNP v1 · sarness-engine/0.4.0 · API 1.4 · events, files');
    expect(describeLimits(s.limits)).toBe('frames up to 1 MiB · window 256 KiB/4 MiB · 64 streams');
    expect(transportLabel('snp')).toBe('SNP');
  });

  it('validates the form per transport', () => {
    const base = { host: 'box', transport: 'snp' as const, port: 8742, snp_port: 8744, snp_pin: PIN };
    expect(validateForm(base)).toBe('');
    expect(validateForm({ ...base, host: ' ' })).toMatch(/address/);
    expect(validateForm({ ...base, snp_port: 0 })).toMatch(/port/);
    expect(validateForm({ ...base, snp_pin: 'sha256:zz' })).toMatch(/identity/);
    // The HTTP port is irrelevant to SNP and vice versa; the pin only matters for SNP.
    expect(validateForm({ ...base, port: 0 })).toBe('');
    expect(validateForm({ ...base, transport: 'http', snp_pin: 'junk' })).toBe('');
    expect(validateForm({ ...base, transport: 'http', port: 70000 })).toMatch(/port/);
  });
});
