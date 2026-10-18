<script lang="ts">
  import { Clapperboard, Image, MessagesSquare, Workflow } from '@lucide/svelte';
  import logo from '../assets/logo.svg';
  import ServerForm from '../components/ServerForm.svelte';
  import { isTauri } from '../api/transport';
</script>

<div class="onb">
  <div class="intro">
    <img src={logo} alt="" width="64" height="64" />
    <h1>Welcome to Sarness</h1>
    <p class="lead">Your self-hosted AI workstation. Chat with local models, generate images and video, and build node-based workflows — all running on your own GPU server.</p>
    <ul class="feats">
      <li><MessagesSquare /> Streaming chat with GGUF models via llama.cpp</li>
      <li><Image /> Image generation, editing and inpainting</li>
      <li><Clapperboard /> Text-to-video on consumer GPUs</li>
      <li><Workflow /> Executable node workflows behind every generation</li>
    </ul>
    <p class="faint small">Sarness talks to a <b>Sarness Engine</b> running on your AI server (or this computer). Traffic goes directly between this app and your server — nothing is routed through Sarness or any cloud service.</p>
  </div>
  <div class="card">
    <h2>Connect to your server</h2>
    <p class="muted small">Enter the engine address and API token, or install the engine over SSH.</p>
    {#if isTauri}
      <ServerForm />
    {:else}
      <p class="muted">Running outside the desktop app. Set VITE_ENGINE_URL and VITE_ENGINE_TOKEN for UI development.</p>
    {/if}
  </div>
</div>

<style>
  .onb {
    height: 100%;
    overflow-y: auto;
    display: grid;
    grid-template-columns: minmax(300px, 440px) minmax(420px, 680px);
    gap: 56px;
    align-content: center;
    justify-content: center;
    padding: 48px;
    background: radial-gradient(80% 60% at 15% 10%, rgba(var(--accent-rgb), 0.14), transparent 60%), var(--bg-0);
  }
  .intro { display: flex; flex-direction: column; gap: 16px; align-self: center; animation: rise-in 400ms var(--ease-out); }
  .intro img { filter: drop-shadow(0 10px 30px rgba(var(--accent-rgb), 0.45)); }
  h1 { font-size: var(--fs-3xl); }
  .lead { color: var(--text-1); font-size: var(--fs-lg); line-height: 1.6; }
  .feats { list-style: none; padding: 0; margin: 4px 0; display: flex; flex-direction: column; gap: 10px; }
  .feats li { display: flex; align-items: center; gap: 10px; color: var(--text-1); }
  .feats :global(svg) { width: 17px; height: 17px; color: var(--lavender); }
  .small { font-size: var(--fs-sm); line-height: 1.55; }
  .card {
    padding: 26px;
    border-radius: var(--r-xl);
    background: var(--bg-2);
    border: 1px solid var(--line-1);
    box-shadow: var(--shadow-3);
    display: flex;
    flex-direction: column;
    gap: 6px;
    animation: rise-in 500ms var(--ease-out);
  }
  .card h2 { margin-bottom: 2px; }
  .card > :global(.form) { margin-top: 14px; }
  @media (max-width: 1080px) { .onb { grid-template-columns: 1fr; gap: 28px; } }
</style>
