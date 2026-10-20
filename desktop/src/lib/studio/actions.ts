// Output actions shared by studios, library and detail panes. Each acts on the server that stores
// the generation (`g.engine`; the primary for generations loaded before multi-server support).
import { invoke } from '@tauri-apps/api/core';
import { save } from '@tauri-apps/plugin-dialog';
import { revealItemInDir } from '@tauri-apps/plugin-opener';
import { apiFor } from '../api/client';
import { isTauri, mediaUrl } from '../api/transport';
import type { Generation } from '../api/types';
import { toast, toastError } from '../stores/toast.svelte';
import { slug } from '../util/format';

function baseName(g: Generation) {
  const stamp = new Date(g.created_at * 1000).toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `${slug(g.prompt || g.type, 48)}-${stamp}`;
}

function ext(g: Generation) {
  const mime = g.file?.mime ?? (g.type === 'video' ? 'video/mp4' : 'image/png');
  return { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif', 'video/mp4': 'mp4', 'video/webm': 'webm' }[mime] ?? 'bin';
}

/** Saves into ~/Pictures/Sarness or ~/Videos/Sarness and returns the local path. */
export async function saveToFolder(g: Generation): Promise<string | null> {
  if (!isTauri) {
    window.open(mediaUrl(g.url, g.engine), '_blank');
    return null;
  }
  try {
    const path = await invoke<string>('save_to_library_folder', { fileId: g.file_id, kind: g.type, name: baseName(g), engine: g.engine ?? null });
    toast('success', 'Saved', path, { action: { label: 'Show', run: () => revealItemInDir(path) } });
    return path;
  } catch (e) {
    toastError(e, 'Save failed');
    return null;
  }
}

export async function exportAs(g: Generation) {
  if (!isTauri) return saveToFolder(g);
  const e = ext(g);
  const dest = await save({ defaultPath: `${baseName(g)}.${e}`, filters: [{ name: g.type === 'video' ? 'Video' : 'Image', extensions: [e] }] });
  if (!dest) return;
  try {
    await invoke('export_file', { fileId: g.file_id, dest, engine: g.engine ?? null });
    toast('success', 'Exported', dest, { action: { label: 'Show', run: () => revealItemInDir(dest) } });
  } catch (err) {
    toastError(err, 'Export failed');
  }
}

export async function exportMetadata(g: Generation) {
  const meta = await apiFor(g.engine).get(`/v1/generations/${g.id}/metadata`);
  const text = JSON.stringify(meta, null, 2);
  if (!isTauri) {
    const blob = new Blob([text], { type: 'application/json' });
    window.open(URL.createObjectURL(blob), '_blank');
    return;
  }
  const dest = await save({ defaultPath: `${baseName(g)}.json`, filters: [{ name: 'JSON', extensions: ['json'] }] });
  if (!dest) return;
  await invoke('write_text_file', { dest, contents: text });
  toast('success', 'Metadata exported', dest);
}

export async function openInFolder(g: Generation) {
  const path = await saveToFolder(g);
  if (path) await revealItemInDir(path);
}

export async function toggleFavorite(g: Generation) {
  const updated = await apiFor(g.engine).patch<Generation>(`/v1/generations/${g.id}`, { favorite: !g.favorite });
  g.favorite = updated.favorite;
}

export async function trash(g: Generation) {
  const a = apiFor(g.engine);
  await a.del(`/v1/generations/${g.id}`);
  toast('info', 'Moved to trash', undefined, {
    action: { label: 'Undo', run: () => a.post('/v1/generations/bulk', { ids: [g.id], action: 'restore' }) },
  });
}

export async function copyImage(g: Generation) {
  try {
    const blob = await (await fetch(mediaUrl(g.url, g.engine))).blob();
    await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
    toast('success', 'Image copied');
  } catch (e) {
    toastError(e, 'Copy failed');
  }
}
