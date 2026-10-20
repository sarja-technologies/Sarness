import { describe, expect, it } from 'vitest';
import { loadModels, models } from './models.svelte';

describe('loadModels', () => {
  it('leaves the lists untouched while no server is online', async () => {
    const before = { installed: models.installed, chatModels: models.chatModels, downloads: models.downloads };
    await loadModels();
    // A rebuild would replace these arrays and re-run every effect reading them (the start-up freeze).
    expect(models.installed).toBe(before.installed);
    expect(models.chatModels).toBe(before.chatModels);
    expect(models.downloads).toBe(before.downloads);
    expect(models.loaded).toBe(false);
  });
});
