import { engineBase, transport } from './transport';
import type { ChatEvent } from './types';

export class ApiError extends Error {
  status: number;
  issues?: { message: string; node_id: string | null }[];
  constructor(status: number, message: string, issues?: ApiError['issues']) {
    super(message);
    this.status = status;
    this.issues = issues;
  }
  get offline() {
    return this.status === 0 || this.status === 502 || this.status === 503;
  }
}

function errorMessage(body: any, status: number): string {
  if (body && typeof body.detail === 'string') return body.detail;
  if (body && Array.isArray(body.detail)) return body.detail.map((d: any) => d.msg).join('; ');
  if (status === 0) return 'Cannot reach the Sarness desktop proxy';
  return `Request failed (${status})`;
}

export async function request<T = any>(method: string, path: string, body?: unknown, init: RequestInit = {}, engine?: string | null): Promise<T> {
  const t = transport();
  if (!t.base) throw new ApiError(0, 'Not connected to a server');
  const headers: Record<string, string> = { ...t.headers };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(engineBase(engine) + path, { method, headers, body: payload, ...init });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError(0, 'Cannot reach the Sarness Engine');
  }
  const text = await res.text();
  let data: any = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(data, res.status), data?.issues);
  return data as T;
}

export type Upload = { id: string; url: string; mime: string; width?: number; height?: number; original_name?: string; size: number };

/** The JSON API of one engine (a server profile id), or of the primary engine when `engine` is null. */
export function apiFor(engine: string | null | undefined) {
  return {
    engine: engine ?? null,
    get: <T = any>(p: string, init?: RequestInit) => request<T>('GET', p, undefined, init, engine),
    post: <T = any>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}, {}, engine),
    put: <T = any>(p: string, b?: unknown) => request<T>('PUT', p, b, {}, engine),
    patch: <T = any>(p: string, b?: unknown) => request<T>('PATCH', p, b, {}, engine),
    del: <T = any>(p: string) => request<T>('DELETE', p, undefined, {}, engine),
    text: async (p: string): Promise<string> => {
      const t = transport();
      const res = await fetch(engineBase(engine) + p, { headers: t.headers });
      if (!res.ok) throw new ApiError(res.status, `Request failed (${res.status})`);
      return res.text();
    },
    upload: async (file: File | Blob, name?: string) => {
      const fd = new FormData();
      fd.append('file', file, name ?? (file as File).name ?? 'upload');
      return request<Upload>('POST', '/v1/uploads', fd, {}, engine);
    },
  };
}
export type EngineApi = ReturnType<typeof apiFor>;

/** The primary engine. */
export const api = apiFor(null);

export function qs(params: Record<string, unknown>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

/** POSTs and parses a Server-Sent Events stream of chat events. */
export async function streamChat(path: string, body: unknown, onEvent: (e: ChatEvent) => void, signal?: AbortSignal, engine?: string | null): Promise<void> {
  const t = transport();
  let res: Response;
  try {
    res = await fetch(engineBase(engine) + path, {
      method: 'POST',
      headers: { ...t.headers, 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') return;
    throw new ApiError(0, 'Cannot reach the Sarness Engine');
  }
  if (!res.ok || !res.body) {
    let data: any;
    try {
      data = await res.json();
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, errorMessage(data, res.status));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let finished = false; // a terminal `done`/`error` event arrived
  const lost = () => new ApiError(0, 'Connection to the engine was interrupted while the model was replying. The reply may be incomplete.');
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          if (line.startsWith('data:')) {
            try {
              const ev = JSON.parse(line.slice(5).trim());
              if (ev?.type === 'done' || ev?.type === 'error') finished = true;
              onEvent(ev);
            } catch {
              /* malformed line */
            }
          }
        }
      }
    }
  } catch (e) {
    if ((e as Error).name === 'AbortError') return;
    throw lost();
  }
  if (!finished && !signal?.aborted) throw lost();
}
