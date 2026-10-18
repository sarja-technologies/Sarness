import { api } from '../api/client';
import type { Conversation } from '../api/types';
import { createSessionObject, hydrate, missingFromServer, type Session } from './sessions';

const KEY = 'sarness.sessions.v1';

function load(): Session[] {
  try {
    return hydrate(JSON.parse(localStorage.getItem(KEY) ?? '[]'));
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

/** Bring engine conversations that predate sessions into history (idempotent). */
export async function syncServerConversations() {
  const convs = await api.get<Conversation[]>('/v1/chat/conversations');
  const add = missingFromServer(store.list, convs);
  if (add.length) store.list.push(...add);
  for (const s of store.list) {
    const c = s.convId ? convs.find((x) => x.id === s.convId) : null;
    if (c && c.title && (!s.titled || s.title !== c.title) && c.title !== 'New conversation') {
      s.title = c.title;
      s.titled = true;
    }
  }
  store.synced = true;
}
