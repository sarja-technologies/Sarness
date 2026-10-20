import { apiFor } from '../api/client';
import { isTauri } from '../api/transport';
import type { Conversation } from '../api/types';
import { conn } from './connection.svelte';
import { adoptLegacy, createSessionObject, hydrate, missingFromServer, type Session } from './sessions';

// v2 records the server of every conversation and generation. v1 is read once and left untouched,
// so an older Sarness can still open it.
const KEY = 'sarness.sessions.v2';
const LEGACY_KEY = 'sarness.sessions.v1';

function load(): Session[] {
  try {
    return hydrate(JSON.parse(localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY) ?? '[]'));
  } catch {
    return [];
  }
}

export const store = $state({ list: load() as Session[], synced: false });

let timer: ReturnType<typeof setTimeout> | null = null;
$effect.root(() => {
  $effect(() => {
    const snap = JSON.stringify($state.snapshot(store.list));
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      try {
        localStorage.setItem(KEY, snap);
      } catch {
        /* storage unavailable */
      }
    }, 200);
  });
});

export function getSession(id: string | null | undefined): Session | undefined {
  return id ? store.list.find((s) => s.id === id) : undefined;
}

export function newSession(): Session {
  const s = createSessionObject();
  store.list.unshift(s);
  return getSession(s.id)!; // the reactive proxy, not the raw object
}

export function removeSession(id: string) {
  store.list = store.list.filter((s) => s.id !== id);
}

export function touch(s: Session) {
  s.updated = Date.now() / 1000;
}

/** Assigns legacy (pre multi-server) data to the server it came from: the one connected at the time. */
export function adoptLegacySessions(engine: string) {
  adoptLegacy(store.list, engine);
}

/** Bring conversations of one engine (default: every online engine) that predate sessions into history (idempotent). */
export async function syncServerConversations(engine?: string | null) {
  const ids = engine ? [engine] : Object.keys(conn.engines).filter((id) => conn.engines[id].state === 'online');
  for (const id of ids) {
    const convs = await apiFor(isTauri ? id : null).get<Conversation[]>('/v1/chat/conversations');
    const add = missingFromServer(store.list, convs, id);
    if (add.length) store.list.push(...add);
    for (const s of store.list) {
      if (s.engine && s.engine !== id) continue;
      const c = s.convId ? convs.find((x) => x.id === s.convId) : null;
      if (c && c.title && (!s.titled || s.title !== c.title) && c.title !== 'New conversation') {
        s.title = c.title;
        s.titled = true;
      }
    }
  }
  store.synced = true;
}
