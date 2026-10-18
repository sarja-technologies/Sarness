import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Shown in Settings → About so it is obvious which build is running.
function buildStamp() {
  try {
    const sha = execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    const dirty = execSync('git status --porcelain', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim() ? '+local' : '';
    return `${sha}${dirty} · ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;
  } catch {
    return 'unknown';
  }
}

const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  plugins: [svelte()],
  resolve: {
    conditions: process.env.VITEST ? ['browser'] : undefined,
    alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) },
  },
  define: { __SARNESS_BUILD__: JSON.stringify(buildStamp()) },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host ? { protocol: 'ws', host, port: 1421 } : undefined,
    watch: { ignored: ['**/src-tauri/**'] },
  },
  build: {
    target: 'es2022',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  test: {
    // a DOM environment makes vitest compile .svelte.ts for the client, so rune effects (persistence) really run.
    environment: 'happy-dom',
    include: ['src/**/*.test.ts'],
  },
});
