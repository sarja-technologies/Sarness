<script lang="ts">
  import { Clapperboard, FolderOpen, Image, ListTodo, MessagesSquare, Play, Plus, RotateCcw, Square, Trash2, Workflow } from '@lucide/svelte';
  import { api, qs } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import type { Conversation, Generation, Job, Page } from '../api/types';
  import SystemPanel from '../components/SystemPanel.svelte';
  import { activeProfile, conn, onEvent, refreshSystem } from '../stores/connection.svelte';
  import { cancelJob, jobs, onGeneration, recentJobs, retryJob } from '../stores/jobs.svelte';
  import { models } from '../stores/models.svelte';
  import { prefs, persistPrefs, type View } from '../stores/prefs.svelte';
  import { openSession, startNewConversation } from '../stores/panes.svelte';
  import { store as sessionStore } from '../stores/sessions.svelte';
  import { createProject, projects, selectProject } from '../stores/projects.svelte';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Empty from '../ui/Empty.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Progress from '../ui/Progress.svelte';
  import { promptDialog } from '../ui/dialogs.svelte';
  import { toastError } from '../stores/toast.svelte';
  import { ago, duration } from '../util/format';

  let recent = $state<Generation[]>([]);
  let convs = $state<Conversation[]>([]);
  let loaded = '';

  $effect(() => {
    const key = `${conn.state}:${conn.activeId}`;
    if (conn.state === 'online' && loaded !== key) {
      loaded = key;
      api.get<Page<Generation>>(`/v1/generations${qs({ limit: 12 })}`).then((p) => (recent = p.items)).catch(() => {});
      api.get<Conversation[]>('/v1/chat/conversations').then((c) => (convs = c.slice(0, 6))).catch(() => {});
    }
  });
  onGeneration((g) => (recent = [g, ...recent].slice(0, 12)));
  onEvent('connection.restored', () => refreshSystem());

  // Refresh slow-changing system info periodically while visible.
  $effect(() => {
    if (prefs.view !== 'system') return;
    const t = setInterval(() => conn.state === 'online' && refreshSystem(), 20000);
    return () => clearInterval(t);
  });

  function go(v: View) {
    prefs.view = v;
    persistPrefs();
  }

  function open(mode: string) {
    startNewConversation();
    go('workspace');
  }

  const queue = $derived(recentJobs(14));
  const hour = new Date().getHours();
  const greeting = hour < 5 ? 'Good night' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';

  async function newProject() {
    const n = await promptDialog('New project', '', { confirmLabel: 'Create' });
    if (!n) return;
    try {
      const p = await createProject(n);
      selectProject(p.id);
    } catch (e) {
      toastError(e);
    }
  }

  const statusTone = (s: Job['status']) => ({ queued: 'neutral', running: 'accent', completed: 'ok', failed: 'err', cancelled: 'warn' } as const)[s];
</script>

<div class="ws">
  <div class="inner">
    <header>
      <div>
        <h1>{greeting}</h1>
        <p class="muted">
          {#if conn.state === 'online' && conn.system}
            Connected to <b>{activeProfile()?.name}</b> · {conn.system.hardware.hostname} · {models.installed.length} models installed
          {:else if conn.state === 'offline'}
            The server is offline. Sarness keeps retrying; cached results remain viewable.
          {:else}
            Connecting…
          {/if}
        </p>
      </div>
    </header>

    <div class="quick">
      <button class="qa" onclick={() => open('image')}><span class="qi"><Image /></span><span><b>Create an image</b><small>Prompt, edit or inpaint</small></span></button>
      <button class="qa" onclick={() => open('video')}><span class="qi"><Clapperboard /></span><span><b>Generate a video</b><small>Text or image to video</small></span></button>
      <button class="qa" onclick={() => open('chat')}><span class="qi"><MessagesSquare /></span><span><b>Start a chat</b><small>Local LLMs with streaming</small></span></button>
      <button class="qa" onclick={() => open('image')}><span class="qi"><Workflow /></span><span><b>Build a workflow</b><small>Switch an image session to Advanced</small></span></button>
    </div>

    {#if conn.system}
      <section>
        <div class="sh"><h2>Server</h2></div>
        <SystemPanel system={conn.system} stats={conn.stats} />
      </section>
    {/if}

    <div class="cols">
      <section class="queue">
        <div class="sh"><h2>Queue</h2>
          {#if queue.some((j) => ['completed', 'failed', 'cancelled'].includes(j.status))}
            <IconButton label="Clear finished jobs" size="sm" onclick={async () => { await api.del('/v1/jobs/finished'); for (const j of Object.values(jobs.byId)) if (['completed','failed','cancelled'].includes(j.status)) delete jobs.byId[j.id]; }}><Trash2 /></IconButton>
          {/if}
        </div>
        {#if !queue.length}
          <Empty icon={ListTodo} title="Queue is empty" body="Image and video jobs appear here with live progress." compact />
        {/if}
        <div class="jobs">
          {#each queue as j (j.id)}
            {@const p = jobs.progress[j.id]}
            <div class="job">
              <span class="jk">{#if j.kind === 'video'}<Clapperboard />{:else}<Image />{/if}</span>
              <div class="jinfo">
                <div class="jt truncate">{j.title}</div>
                <div class="js">
                  <Badge tone={statusTone(j.status)}>{j.status}</Badge>
                  {#if j.status === 'queued' && j.queue_position}<span>#{j.queue_position} in queue</span>{/if}
                  {#if j.waiting_reason}<span>{j.waiting_reason}</span>{/if}
                  {#if j.status === 'running'}<span class="truncate">{p?.text ?? j.progress_text}</span>{/if}
                  {#if j.status === 'completed' && j.result}<span>{duration(j.result.elapsed_s)} · peak {(j.result.peak_vram_mb / 1024).toFixed(1)} GB VRAM</span>{/if}
                  {#if j.status === 'failed'}<span class="err truncate" title={j.error?.message}>{j.error?.message}</span>{/if}
                  <span class="faint">{ago(j.created_at)}</span>
                </div>
                {#if j.status === 'running'}<Progress value={p?.progress ?? j.progress} />{/if}
              </div>
              {#if j.status === 'running' || j.status === 'queued'}
                <IconButton label="Cancel" size="sm" tone="danger" onclick={() => cancelJob(j.id)}><Square /></IconButton>
              {:else if j.status === 'failed' || j.status === 'cancelled'}
                <IconButton label="Retry" size="sm" onclick={() => retryJob(j.id)}><RotateCcw /></IconButton>
              {/if}
            </div>
          {/each}
        </div>
      </section>

      <section>
        <div class="sh"><h2>Recent work</h2><Button size="sm" variant="ghost" onclick={() => go('library')}>Open library</Button></div>
        {#if recent.length}
          <div class="recent">
            {#each recent as g (g.id)}
              <button class="rthumb" onclick={() => go('library')} title={g.prompt ?? ''}>
                <img src={mediaUrl(g.thumb_url)} alt="" loading="lazy" />
                {#if g.type === 'video'}<span class="pl"><Play /></span>{/if}
              </button>
            {/each}
          </div>
        {:else}
          <Empty icon={Image} title="No generations yet" compact />
        {/if}

        <div class="sh" style="margin-top:22px"><h2>Conversations</h2><Button size="sm" variant="ghost" onclick={() => open('chat')}>Open chat</Button></div>
        <div class="convs">
          {#each convs as c (c.id)}
            <button class="conv" onclick={() => { const s = sessionStore.list.find((x) => x.convId === c.id); if (s) { openSession(s.id); go('workspace'); } }}><MessagesSquare /><span class="truncate">{c.title}</span><span class="faint">{ago(c.updated_at)}</span></button>
          {/each}
          {#if !convs.length}<p class="faint small">No conversations yet.</p>{/if}
        </div>

        <div class="sh" style="margin-top:22px"><h2>Projects</h2><IconButton label="New project" size="sm" onclick={newProject}><Plus /></IconButton></div>
        <div class="projects">
          {#each projects.list as p (p.id)}
            <button class="proj" class:on={prefs.projectId === p.id} onclick={() => selectProject(prefs.projectId === p.id ? null : p.id)}>
              <FolderOpen /><span class="truncate">{p.name}</span><span class="faint">{p.generation_count} items · {p.folders.length} folders</span>
            </button>
          {/each}
          {#if !projects.list.length}<p class="faint small">Projects group generations and chats. Select one in the sidebar to save new work into it.</p>{/if}
        </div>
      </section>
    </div>
  </div>
</div>

<style>
  .ws { height: 100%; overflow-y: auto; background: radial-gradient(70% 40% at 20% 0%, rgba(var(--accent-rgb), 0.08), transparent 70%); }
  .inner { max-width: 1480px; margin: 0 auto; padding: 30px 36px 48px; display: flex; flex-direction: column; gap: 26px; }
  header h1 { font-size: 28px; margin-bottom: 4px; }
  header p { font-size: var(--fs-md); }
  .quick { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  .qa { display: flex; align-items: center; gap: 14px; padding: 16px; border-radius: var(--r-lg); border: 1px solid var(--line-1); background: linear-gradient(180deg, var(--bg-3), var(--bg-2)); color: var(--text-1); text-align: left; cursor: pointer; transition: border-color var(--t-1), transform var(--t-2) var(--ease), box-shadow var(--t-2); }
  .qa:hover { border-color: var(--select-line); transform: translateY(-2px); box-shadow: 0 14px 36px rgba(0, 0, 0, 0.4); }
  .qi { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 12px; background: var(--accent-ghost); color: var(--lavender); flex-shrink: 0; }
  .qi :global(svg) { width: 19px; height: 19px; }
  .qa b { display: block; color: var(--text-0); font-weight: 600; }
  .qa small { color: var(--text-2); font-size: var(--fs-sm); }
  .sh { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .sh h2 { font-size: var(--fs-md); text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-2); }
  .cols { display: grid; grid-template-columns: minmax(380px, 1.1fr) 1fr; gap: 26px; align-items: start; }
  .jobs { display: flex; flex-direction: column; gap: 6px; }
  .job { display: flex; align-items: center; gap: 12px; padding: 11px 12px; border-radius: var(--r-lg); background: var(--bg-2); border: 1px solid var(--line-0); animation: fade-in var(--t-2); }
  .jk { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; background: var(--bg-4); color: var(--text-2); flex-shrink: 0; }
  .jk :global(svg) { width: 15px; height: 15px; }
  .jinfo { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
  .jt { color: var(--text-0); font-weight: 500; }
  .js { display: flex; align-items: center; gap: 8px; font-size: var(--fs-xs); color: var(--text-2); min-width: 0; }
  .err { color: var(--err); min-width: 0; }
  .js .faint { white-space: nowrap; flex-shrink: 0; }
  .recent { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
  .rthumb { position: relative; aspect-ratio: 1; padding: 0; border: 0; border-radius: var(--r-md); overflow: hidden; background: var(--bg-3); cursor: pointer; transition: transform var(--t-2) var(--ease); }
  .rthumb:hover { transform: scale(1.03); }
  .rthumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .pl { position: absolute; right: 5px; bottom: 5px; display: grid; place-items: center; width: 18px; height: 18px; border-radius: 6px; background: rgba(0, 0, 0, 0.6); color: white; }
  .pl :global(svg) { width: 10px; height: 10px; fill: currentColor; }
  .convs, .projects { display: flex; flex-direction: column; gap: 4px; }
  .conv, .proj { display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: var(--r-md); border: 1px solid transparent; background: var(--bg-2); color: var(--text-1); cursor: pointer; text-align: left; }
  .conv:hover, .proj:hover { border-color: var(--line-2); }
  .proj.on { border-color: var(--select-line); background: var(--select-bg); }
  .conv :global(svg), .proj :global(svg) { width: 15px; height: 15px; color: var(--text-3); flex-shrink: 0; }
  .conv .truncate, .proj .truncate { flex: 1; }
  .small { font-size: var(--fs-sm); }
  @media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
</style>
