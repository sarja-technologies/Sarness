// Conversation-level actions shared by the pane header and the sidebar.
import { invoke } from '@tauri-apps/api/core';
import { save as saveDialog } from '@tauri-apps/plugin-dialog';
import { api } from '../api/client';
import { isTauri } from '../api/transport';
import { dropAgent } from '../agent/state.svelte';
import { removeSession } from '../stores/sessions.svelte';
import type { Session } from '../stores/sessions';
import { toast, toastError } from '../stores/toast.svelte';
import { confirmDialog, promptDialog } from '../ui/dialogs.svelte';

export async function renameSession(s: Session) {
  const t = await promptDialog('Rename conversation', s.title, { confirmLabel: 'Rename' });
  if (!t) return;
  s.title = t;
  s.titled = true;
  if (s.convId) api.patch(`/v1/chat/conversations/${s.convId}`, { title: t }).catch(() => {});
}

/** Returns true when the conversation was deleted. Generated media stays in the Library. */
export async function deleteSession(s: Session): Promise<boolean> {
  const body = s.convId
    ? 'The chat messages are permanently deleted from the server. Generated images and videos stay in your Library.'
    : 'This conversation is removed from your history. Generated images and videos stay in your Library.';
  const note = ' Coding task history is removed from Sarness; files in your project folders are never touched.';
  if (!(await confirmDialog(`Delete “${s.title}”?`, body + (s.hasAgent ? note : ''), { danger: true, confirmLabel: 'Delete' }))) return false;
  try {
    if (s.convId) await api.del(`/v1/chat/conversations/${s.convId}`);
  } catch (e) {
    toastError(e, 'Could not delete the conversation');
    return false;
  }
  dropAgent(s.id);
  removeSession(s.id);
  return true;
}

export async function exportSession(s: Session, fmt: 'json' | 'md') {
  if (!s.convId) return toast('info', 'Nothing to export yet', 'Only chat and code turns are exported; generated media lives in the Library.');
  try {
    const data = fmt === 'md'
      ? await api.text(`/v1/chat/conversations/${s.convId}/export?format=md`)
      : JSON.stringify(await api.get(`/v1/chat/conversations/${s.convId}/export`), null, 2);
    const name = `${s.title.replace(/[^\w.-]+/g, '-').slice(0, 60)}.${fmt}`;
    if (isTauri) {
      const dest = await saveDialog({ defaultPath: name });
      if (!dest) return;
      await invoke('write_text_file', { dest, contents: data });
      toast('success', 'Conversation exported', dest);
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([data]));
      a.download = name;
      a.click();
    }
  } catch (e) {
    toastError(e, 'Export failed');
  }
}
