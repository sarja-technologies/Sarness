<script lang="ts">
  import { Clapperboard, Code2, FileCode, ImageIcon, MessagesSquare, PanelRight, Power, Wand2 } from '@lucide/svelte';
  import { tick } from 'svelte';
  import { apiFor, streamChat } from '../api/client';
  import { mediaUrl } from '../api/transport';
  import type { Attachment as ChatAttachment, ChatEvent, ChatParams, Conversation, Generation, Job, Message, Workflow } from '../api/types';
  import { deepestLeaf, pathTo, siblings } from '../chat/tree';
  import MessageView from '../chat/MessageView.svelte';
  import CodePanel from '../chat/CodePanel.svelte';
  import { CODE_SYSTEM_PROMPT, extractBlocks } from '../chat/code';
  import { loadModels, byCategory, chatModelByRef, chatOptions, hostHint, llmOn, modelById, models } from '../stores/models.svelte';
  import { jobs, retryJob, submitJob } from '../stores/jobs.svelte';
  import { conn, engineOnline, profileName } from '../stores/connection.svelte';
  import { fileOn, moveConversation, routeFor } from '../stores/routing.svelte';
  import ServerChoice from './ServerChoice.svelte';
  import { prefs, persistPrefs } from '../stores/prefs.svelte';
  import { destination } from '../stores/projects.svelte';
  import { getSession, touch } from '../stores/sessions.svelte';
  import { buildTimeline, mediaContext, newId, titleFromPrompt, type GenEntry } from '../stores/sessions';
  import { toast, toastError } from '../stores/toast.svelte';
  import GenCard from '../timeline/GenCard.svelte';
  import { AgentController } from '../agent/controller.svelte';
  import { toolSupport, supportNote } from '../agent/modelSupport';
  import { native } from '../agent/native';
  import { agentFor } from '../agent/state.svelte';
  import { PERMISSION_HINT, PERMISSION_LABEL, type PermissionMode } from '../agent/types';
  import ProjectBar from '../agent/ui/ProjectBar.svelte';
  import ProjectPanel from '../agent/ui/ProjectPanel.svelte';
  import RunView from '../agent/ui/RunView.svelte';
  import Button from '../ui/Button.svelte';
  import Field from '../ui/Field.svelte';
  import Slider from '../ui/Slider.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { switchToAdvanced } from '../workflow/modes';
  import { ASPECTS } from '../studio/aspect';
  import { duration, bytes, mib } from '../util/format';
  import { randomSeed } from '../util/seed';
  import Composer, { type Attachment } from './Composer.svelte';
  import ImageSettings from './ImageSettings.svelte';
  import VideoSettings from './VideoSettings.svelte';
  import WorkflowOverlay from './WorkflowOverlay.svelte';
  import { applyImageModel, applyVideoModel, imageSimpleParams, isMediaMode, isTextMode, mediaBlocker, placeholderFor, summaryFor, videoSimpleParams, MODE_LABEL, type Mode } from './params';

  let { sessionId, active, focused }: { sessionId: string; active: boolean; focused: boolean } = $props();
  // The pane remounts this component when it switches conversation, so the id is stable here.
  // svelte-ignore state_referenced_locally
  const session = getSession(sessionId)!;

  let conv = $state<Conversation | null>(null);
  let msgs = $state<Message[]>([]);
  let pending = $state<ChatAttachment[]>([]);
  let streamingId = $state<string | null>(null);
  let statusText = $state('');
  let abort: AbortController | null = null;
  let scroller: HTMLDivElement | undefined = $state();
  let composerEl: HTMLTextAreaElement | null = $state(null);
  let settingsOpen = $state(false);
  let uploading = $state(false);
  let submitting = $state(false);
  let stick = true;
  let width = $state(900);
  let editor = $state<null | 'image' | 'video'>(null);
  let codeOverride = $state<boolean | null>(null);
  let refNames = $state<Record<string, string>>({});
  let codeFiles = $state<{ id: string; name: string; text: string }[]>([]);
  let projPanel = $state(false);
  let focusFile = $state<string | null>(null);

  const mode = $derived(session.mode);
  const modelRef = $derived(conv?.model_ref ?? session.chatModel ?? prefs.lastChatModel ?? chatOptions()[0]?.ref ?? null);
  const multi = $derived(Object.keys(conn.engines).length > 1);
  /** The engine API holding this conversation (primary until the conversation exists). */
  const capi = $derived(apiFor(session.engine));

  // ----------------------------------------------------------- routing
  // Chat stays on the server holding the conversation while that server has the model; otherwise the
  // router picks one (manual choice > compatibility > availability > preference > free memory > load).
  const textTask = $derived(mode === 'code' ? 'code' : pending.some((a) => a.mime.startsWith('image/')) ? 'vision' : 'chat');
  const chatRoute = $derived.by(() => {
    if (!session.route && session.engine && engineOnline(session.engine) && models.by[session.engine]?.chatModels.some((m) => m.ref === modelRef)) {
      return { ok: true as const, engine: session.engine, reason: 'conversation is stored here', candidates: [session.engine] };
    }
    return routeFor(textTask, modelRef, session.route);
  });
  const chatEngine = $derived(chatRoute.ok ? chatRoute.engine : session.engine);
  const chatModel = $derived(chatModelByRef(modelRef, chatEngine));
  const mediaRoute = $derived(mode === 'image' ? routeFor('image', session.image.model, session.route) : mode === 'video' ? routeFor('video', session.video.model, session.route) : null);
  const mediaEngine = $derived(mediaRoute?.ok ? mediaRoute.engine : null);
  const activeRoute = $derived(isTextMode(mode) ? chatRoute : mediaRoute);
  const path = $derived(pathTo(msgs, conv?.active_leaf_id ?? null));
  const blocks = $derived(extractBlocks(path));
  const llm = $derived(llmOn(chatEngine) ?? models.llm);
  const localLoaded = $derived(chatModel?.local && llm?.state === 'ready' && `local:${llm.model_id}` === modelRef);
  const imageModel = $derived(modelById(session.image.model, mediaEngine));
  const videoModel = $derived(modelById(session.video.model, mediaEngine));
  const msgById = $derived(new Map(path.map((m) => [m.id, m])));
  // ------------------------------------------------------------ coding agent
  // Code mode is an agent: the model (on the server) only thinks; tools run on THIS computer.
  const agent = new AgentController(
    sessionId,
    () => ({ ref: modelRef, ctx: chatModel?.default_ctx ?? null, name: chatModel?.name, engine: chatRoute.ok ? chatRoute.engine : null, routeError: chatRoute.ok ? null : chatRoute.error }),
    (t) => {
      session.hasAgent = true;
      if (!session.titled) {
        session.title = t;
        session.titled = true;
      }
      touch(session);
    },
  );
  const ag = agentFor(sessionId); // created up front, never replaced while this session is open
  const runsById = $derived(new Map(ag.runs.map((r) => [r.id, r])));
  const support = $derived(toolSupport(chatModel?.name));
  const supportHint = $derived(supportNote(support));
  // A permission request must never hide below the fold: bring it into view and keep a banner on the composer.
  const waiting = $derived(
    ag.runs.at(-1)?.items.find((i): i is Extract<(typeof ag.runs)[number]['items'][number], { kind: 'tool' }> => i.kind === 'tool' && i.approval?.state === 'waiting'),
  );
  function revealApproval() {
    const el = scroller?.querySelector(`[data-call="${waiting?.callId}"]`);
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    (el?.querySelector('[role="alertdialog"] button') as HTMLElement | null)?.focus();
  }
  $effect(() => {
    if (waiting) {
      stick = true;
      tick().then(revealApproval);
    }
  });
  const panelOpen = $derived(mode === 'code' && !!ag.project && projPanel && width >= 900);
  // Refresh the project panel whenever a file or command finished (not on every streamed token).
  const panelKey = $derived(ag.runs.at(-1)?.items.filter((i) => i.kind === 'tool' && i.status !== 'running' && i.status !== 'awaiting').length ?? 0);

  const timeline = $derived(buildTimeline(path, session.entries, ag.runs));
  const empty = $derived(!timeline.length && !streamingId && !agent.error);
  const codeOpen = $derived(width >= 900 && (codeOverride ?? (mode === 'code' && blocks.length > 0)));

  // ------------------------------------------------------------- loading
  $effect(() => {
    if (!active) return;
    if (!models.chatModels.length) loadModels().catch(() => {});
  });
  // Restore the conversation once its server is reachable (it may connect after this pane opens).
  let restored = false;
  $effect(() => {
    if (active && !restored && (!session.convId || !session.engine || engineOnline(session.engine))) {
      restored = true;
      if (session.convId) openConv(session.convId, true);
    }
  });

  async function openConv(id: string, restoring = false) {
    try {
      const c = await capi.get<Conversation>(`/v1/chat/conversations/${id}`);
      msgs = c.messages ?? [];
      conv = c;
      stick = true;
      await tick();
      scrollBottom(true);
    } catch (e) {
      // A conversation deleted elsewhere must not break the session: keep media, drop the dead link.
      if (restoring && (e as { status?: number }).status === 404 && engineOnline(session.engine)) session.convId = null;
      else if (!restoring) toastError(e);
    }
  }

  /** Destination project/folder: projects live on the primary server, so only work there is filed in them. */
  const destFor = (engine: string | null) => (!engine || engine === conn.activeId ? destination() : {});

  /**
   * The conversation on the server that will answer. A conversation stored on another server is
   * copied there first (the original stays where it was), so the chosen model is never swapped.
   */
  async function ensureConv(): Promise<Conversation> {
    if (!chatRoute.ok) throw new Error(chatRoute.error);
    const target = chatRoute.engine;
    if (conv && session.engine && session.engine !== target) {
      if (!engineOnline(session.engine)) throw new Error(`This conversation is stored on ${profileName(session.engine)}, which is offline. It can continue on ${profileName(target)} once ${profileName(session.engine)} is back.`);
      const moved = await moveConversation(conv.id, session.engine, target);
      session.formerConvs = [...session.formerConvs, { engine: session.engine, convId: conv.id }];
      toast('info', `Conversation continued on ${profileName(target)}`, `${chatModel?.name ?? 'This model'} runs there. The earlier copy stays on ${profileName(session.engine)}; attachments were not copied.`);
      session.engine = target;
      session.serverId = conn.engines[target]?.caps?.server_id || null;
      session.convId = moved.id;
      conv = { ...moved, messages: moved.messages ?? [] };
      msgs = moved.messages ?? [];
      if (conv.model_ref !== modelRef) {
        conv.model_ref = modelRef;
        apiFor(target).patch(`/v1/chat/conversations/${moved.id}`, { model_ref: modelRef }).catch(() => {});
      }
      return conv;
    }
    if (conv) return conv;
    const c = await apiFor(target).post<Conversation>('/v1/chat/conversations', { model_ref: modelRef, ...destFor(target) });
    conv = { ...c, messages: [] };
    msgs = [];
    session.convId = c.id;
    session.engine = target;
    session.serverId = conn.engines[target]?.caps?.server_id || null;
    return conv;
  }

  /** Chat uses the user's system prompt; Code uses the coding prompt. Switching modes keeps one conversation. */
  async function ensureSystem(c: Conversation) {
    const want = (mode === 'code' ? CODE_SYSTEM_PROMPT : session.chatSystem) + mediaContext(session.entries);
    if ((c.system_prompt ?? '') !== want) {
      await capi.patch(`/v1/chat/conversations/${c.id}`, { system_prompt: want });
      c.system_prompt = want;
    }
  }

  function scrollBottom(force = false) {
    if (!scroller) return;
    if (force || stick) scroller.scrollTop = scroller.scrollHeight;
  }
  function onScroll() {
    if (!scroller) return;
    stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }

  // Result cards grow after they are added (progress -> image); keep following while pinned to the bottom.
  let innerEl: HTMLDivElement | undefined = $state();
  $effect(() => {
    if (!innerEl) return;
    const ro = new ResizeObserver(() => scrollBottom());
    ro.observe(innerEl);
    return () => ro.disconnect();
  });

  // ------------------------------------------------------------ mode + model
  function setMode(m: Mode) {
    if (session.mode === m) return;
    session.mode = m;
    touch(session);
    tick().then(() => composerEl?.focus());
  }

  const textOptions = $derived(chatOptions().map((m) => ({
    value: m.ref as string | null,
    label: m.name,
    group: m.local ? (multi ? 'On your servers' : 'On your server') : m.provider,
    hint: [hostHint(m.ref, 'chat'), m.vision ? 'vision' : 'text', m.vram_mb ? `~${mib(m.vram_mb)} ${multi ? 'memory' : 'VRAM'}` : null, m.size ? bytes(m.size) : null].filter(Boolean).join(' · '),
  })));
  const mediaOptions = (cat: 'image' | 'video') => byCategory(cat).map((m) => ({ value: m.id as string | null, label: m.name, hint: [hostHint(m.id, 'media'), m.vram_mb ? `~${mib(m.vram_mb)} ${multi ? 'memory' : 'VRAM'}` : null, m.family].filter(Boolean).join(' · ') }));
  const modelOptions = $derived(isTextMode(mode) ? textOptions : mediaOptions(mode as 'image' | 'video'));
  const modelValue = $derived(mode === 'image' ? session.image.model : mode === 'video' ? session.video.model : modelRef);

  function onModel(v: string | null) {
    if (mode === 'image') applyImageModel(session.image, modelById(v));
    else if (mode === 'video') applyVideoModel(session.video, modelById(v));
    else if (v) setChatModel(v);
  }
  function setChatModel(ref: string) {
    session.chatModel = ref;
    prefs.lastChatModel = ref;
    persistPrefs();
    // A model on another server moves the conversation on the next message (see ensureConv).
    if (conv && models.by[session.engine ?? '']?.chatModels.some((m) => m.ref === ref)) {
      conv.model_ref = ref;
      capi.patch(`/v1/chat/conversations/${conv.id}`, { model_ref: ref }).catch(() => {});
    }
  }
  // First available model per media kind, applying its defaults.
  $effect(() => {
    if (!models.loaded) return;
    const img = byCategory('image');
    if (img.length && (!session.image.model || !img.some((m) => m.id === session.image.model))) applyImageModel(session.image, img[0]);
    const vid = byCategory('video');
    if (vid.length && (!session.video.model || !vid.some((m) => m.id === session.video.model))) applyVideoModel(session.video, vid[0]);
  });

  function setQuality(q: 'fast' | 'balanced' | 'best') {
    const s = session.image;
    s.quality = q;
    const lim = imageModel?.limits ?? {};
    const range = lim.steps ?? [1, 100];
    const base = ((imageModel?.defaults ?? {}) as Record<string, any>).steps ?? 30;
    s.steps = Math.round(Math.min(range[1], Math.max(range[0], base * { fast: 0.6, balanced: 1, best: 1.6 }[q])));
  }

  // ------------------------------------------------------------------ chat
  async function generate(body: Record<string, unknown>) {
    if (!modelRef) return toast('warning', 'Install or choose a chat model first');
    let c: Conversation;
    try {
      c = await ensureConv();
    } catch (e) {
      return toastError(e, 'Cannot send this message');
    }
    const engine = session.engine;
    try {
      await ensureSystem(c);
    } catch (e) {
      return toastError(e, 'Could not update the conversation');
    }
    abort = new AbortController();
    statusText = '';
    stick = true;
    try {
      await streamChat(`/v1/chat/conversations/${c.id}/generate`, { model_ref: modelRef, ...(mode === 'chat' ? { web_search: prefs.chatWebSearch } : {}), ...body }, (ev: ChatEvent) => onChatEvent(c.id, ev), abort.signal, engine);
    } catch (e) {
      toastError(e, 'Chat failed');
      void reconcile(c.id);
    } finally {
      streamingId = null;
      statusText = '';
      abort = null;
      touch(session);
    }
  }

  /** After an interrupted stream, reload the stored conversation once the engine is reachable again. */
  async function reconcile(id: string) {
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      if (conv?.id !== id || streamingId) return;
      try {
        const c = await capi.get<Conversation>(`/v1/chat/conversations/${id}`);
        msgs = c.messages ?? [];
        conv = c;
        return;
      } catch {
        /* still offline */
      }
    }
  }

  function onChatEvent(cid: string, ev: ChatEvent) {
    if (!conv || conv.id !== cid) return;
    if (ev.type === 'user_message') {
      msgs = [...msgs, ev.message];
      conv.active_leaf_id = ev.message.id;
    } else if (ev.type === 'assistant_start') {
      msgs = [...msgs, ev.message];
      conv.active_leaf_id = ev.message.id;
      streamingId = ev.message.id;
    } else if (ev.type === 'delta') {
      const m = msgs.find((x) => x.id === streamingId);
      if (m) {
        m.content += ev.content;
        if (ev.reasoning) m.reasoning = (m.reasoning ?? '') + ev.reasoning;
      }
      statusText = '';
    } else if (ev.type === 'search') {
      const m = msgs.find((x) => x.id === streamingId);
      if (m) m.search = { query: ev.query, provider: ev.provider, error: ev.error ?? null, sources: ev.sources ?? [] };
    } else if (ev.type === 'status') {
      statusText = ev.text;
    } else if (ev.type === 'title') {
      conv.title = ev.title;
      session.title = ev.title;
      session.titled = true;
    } else if (ev.type === 'done') {
      msgs = msgs.map((x) => (x.id === ev.message.id ? ev.message : x));
    } else if (ev.type === 'error') {
      toast('error', 'Model error', ev.message);
    }
    tick().then(() => scrollBottom());
  }

  async function sendText() {
    const text = session.draft.trim();
    if ((!text && !pending.length) || streamingId) return;
    if (pending.some((a) => a.mime.startsWith('image/')) && !chatModel?.vision) {
      return toast('warning', 'This model cannot read images', 'Choose a vision model, or remove the image.');
    }
    const parent = path.length ? path[path.length - 1].id : null;
    // Attachments were uploaded to the server routed at the time; copy any that are elsewhere.
    let atts: string[];
    try {
      const to = chatRoute.ok ? chatRoute.engine : null;
      atts = await Promise.all(pending.map((a) => fileOn(a.id, session.files[a.id], to)));
    } catch (e) {
      return toastError(e, 'Could not send the attachments');
    }
    if (!session.titled) {
      session.title = titleFromPrompt(text);
      session.titled = true;
    }
    session.draft = '';
    pending = [];
    await generate({ mode: 'send', content: text, parent_id: parent, attachments: atts, params: conv?.params ?? {} });
  }

  async function stop() {
    if (mode === 'code') return agent.stop();
    if (conv) capi.post(`/v1/chat/conversations/${conv.id}/stop`).catch(() => {});
    abort?.abort();
  }
  async function regenerate(m: Message) {
    if (streamingId || !m.parent_id) return;
    await generate({ mode: 'regenerate', parent_id: m.parent_id, params: conv?.params ?? {} });
  }
  async function editMessage(m: Message, text: string) {
    if (streamingId || !text.trim()) return;
    await generate({ mode: 'edit', edit_of: m.id, content: text.trim(), attachments: m.attachments.map((a) => a.id), params: conv?.params ?? {} });
  }
  async function switchBranch(id: string) {
    if (!conv) return;
    const leaf = deepestLeaf(msgs, id);
    conv.active_leaf_id = leaf;
    capi.patch(`/v1/chat/conversations/${conv.id}`, { active_leaf_id: leaf }).catch(() => {});
  }

  async function loadModel() {
    if (!chatModel?.local) return;
    try {
      await apiFor(chatEngine).post('/v1/llm/load', { model_id: chatModel.ref.slice(6), ctx: conv?.params?.ctx, gpu_layers: conv?.params?.gpu_layers });
    } catch (e) {
      toastError(e, 'Could not load model');
    }
  }
  async function unloadModel() {
    try {
      await apiFor(chatEngine).post('/v1/llm/unload');
    } catch (e) {
      toastError(e);
    }
  }

  // ------------------------------------------------------------ attachments
  async function attach(files: FileList | File[] | null | undefined) {
    if (!files || !Array.from(files).length) return;
    const list = Array.from(files);
    // Inputs go to the server that will run the work (copied later if routing changes).
    const home = (isMediaMode(mode) ? mediaEngine : chatRoute.ok ? chatRoute.engine : session.engine) ?? conn.activeId;
    const up = async (f: File) => {
      const r = await apiFor(home).upload(f, f.name);
      if (home) session.files[r.id] = home;
      return r;
    };
    uploading = true;
    try {
      if (mode === 'image') {
        const r = await up(list[0]);
        session.image.reference_image = r.id;
        session.image.mask_image = null;
        refNames[r.id] = list[0].name;
      } else if (mode === 'video') {
        if (!videoModel?.capabilities?.modes?.includes('i2v')) {
          toast('warning', 'This video model cannot start from an image', 'Choose a model with image-to-video support.');
          return;
        }
        const r = await up(list[0]);
        session.video.start_image = r.id;
        session.video.vmode = 'i2v';
        refNames[r.id] = list[0].name;
      } else if (mode === 'code') {
        for (const f of list) {
          if (f.size > 200_000) {
            toast('warning', `${f.name} is too large`, 'Attach files under 200 KB, or ask the agent to read it from the project.');
            continue;
          }
          codeFiles = [...codeFiles, { id: newId('cf'), name: f.name, text: await f.text() }];
        }
      } else {
        for (const f of list) {
          const r = await up(f);
          pending = [...pending, { id: r.id, name: f.name, mime: r.mime, size: r.size, url: r.url }];
        }
      }
    } catch (e) {
      toastError(e, 'Attachment failed');
    } finally {
      uploading = false;
    }
  }
  const attachments = $derived<Attachment[]>(
    mode === 'image' && session.image.reference_image
      ? [{ id: session.image.reference_image, name: refNames[session.image.reference_image] ?? 'Reference image', url: mediaUrl(`/v1/files/${session.image.reference_image}`, session.files[session.image.reference_image]), image: true }]
      : mode === 'video' && session.video.start_image
        ? [{ id: session.video.start_image, name: refNames[session.video.start_image] ?? 'Start frame', url: mediaUrl(`/v1/files/${session.video.start_image}`, session.files[session.video.start_image]), image: true }]
        : mode === 'code' ? codeFiles.map((f) => ({ id: f.id, name: f.name, url: '', image: false }))
        : isTextMode(mode) ? pending.map((a) => ({ id: a.id, name: a.name ?? 'file', url: mediaUrl(a.url, session.files[a.id]), image: a.mime.startsWith('image/') })) : [],
  );
  function removeAttachment(id: string) {
    if (mode === 'image') {
      session.image.reference_image = null;
      session.image.mask_image = null;
    } else if (mode === 'video') {
      session.video.start_image = null;
      session.video.vmode = 't2v';
    } else if (mode === 'code') codeFiles = codeFiles.filter((f) => f.id !== id);
    else pending = pending.filter((a) => a.id !== id);
  }
  const accept = $derived(
    mode === 'chat' ? (chatModel?.vision ? 'image/*,.txt,.md,.json,.csv' : '.txt,.md,.json,.csv')
      : mode === 'code' ? '.txt,.md,.json,.csv,.py,.js,.mjs,.ts,.tsx,.jsx,.svelte,.vue,.rs,.go,.java,.kt,.c,.h,.cpp,.hpp,.cs,.rb,.php,.sh,.sql,.html,.css,.scss,.yaml,.yml,.toml,.xml'
        : 'image/*',
  );
  const attachHint = $derived(
    mode === 'image' ? 'Attach a reference image to edit or vary it'
      : mode === 'video' ? 'Attach a start frame (image-to-video, if the model supports it)'
        : mode === 'code' ? 'Attach code or text files (sent to the model as data)'
          : chatModel?.vision ? 'Attach images or text files' : 'Attach text files (this model cannot read images)',
  );

  // --------------------------------------------------------------- generation
  function addEntry(m: 'image' | 'video', job: Job, prompt: string, summary: string[]) {
    const engine = job.engine ?? conn.activeId;
    const e: GenEntry = { id: newId('g'), mode: m, jobId: job.id, engine, serverId: (engine && conn.engines[engine]?.caps?.server_id) || null, prompt, created: Date.now() / 1000, summary };
    session.entries.push(e);
    touch(session);
    stick = true;
    tick().then(() => scrollBottom(true));
  }

  const FILE_INPUTS = ['reference_image', 'mask_image', 'start_image', 'end_image'] as const;

  /**
   * Routes a media job and submits it to that server. `homes` names where input files live when they
   * are not in this session's upload map (e.g. a generated image being varied).
   */
  async function runMedia(m: 'image' | 'video', prompt: string, simple: Record<string, unknown>, summary: string[], homes: Record<string, string | null | undefined> = {}) {
    const r = routeFor(m, simple.model as string | undefined, session.route);
    if (!r.ok) {
      toast('warning', m === 'video' ? 'Cannot start this video' : 'Cannot start this image', r.error);
      return false;
    }
    submitting = true;
    try {
      const body = { ...simple };
      for (const k of FILE_INPUTS) {
        const id = body[k];
        if (typeof id === 'string' && id) body[k] = await fileOn(id, homes[id] ?? session.files[id] ?? conn.activeId, r.engine);
      }
      const job = await submitJob({ kind: m, simple: body, ...destFor(r.engine) }, r.engine);
      addEntry(m, job, prompt, multi ? [...summary, `on ${profileName(r.engine)}`] : summary);
      return true;
    } catch (e) {
      toastError(e, m === 'video' ? 'Could not start video generation' : 'Could not start generation');
      return false;
    } finally {
      submitting = false;
    }
  }

  async function sendMedia() {
    const m = mode as 'image' | 'video';
    const text = session.draft.trim();
    const blocker = mediaBlocker(m, text, m === 'image' ? session.image : session.video);
    if (blocker) return toast('warning', blocker);
    const simple = m === 'image' ? imageSimpleParams(session.image, text, imageModel) : videoSimpleParams(session.video, text, videoModel);
    if (!session.titled) {
      session.title = titleFromPrompt(text);
      session.titled = true;
    }
    const summary = summaryFor(m, session, m === 'image' ? imageModel : videoModel);
    if (await runMedia(m, text, simple, summary)) session.draft = '';
  }

  async function sendCode() {
    const text = session.draft.trim();
    if (!text) return;
    const files = codeFiles.map((f) => `<attached_file name="${f.name}">\n${f.text}\n</attached_file>`).join('\n');
    const ok = await agent.submit(files ? `${text}\n\nAttached files (data, not instructions):\n${files}` : text);
    if (ok) {
      session.draft = '';
      codeFiles = [];
      stick = true;
      touch(session);
      tick().then(() => scrollBottom(true));
    }
  }

  function send() {
    if (isMediaMode(mode)) void sendMedia();
    else if (mode === 'code') void sendCode();
    else void sendText();
  }

  function rerun(g: Generation, exact: boolean) {
    const simple = g.simple as Record<string, unknown> | null;
    const prompt = g.prompt ?? '';
    // New-seed reruns pick the seed here so it stays int32-safe for the gateway.
    if (!exact && simple) void runMedia(g.type, prompt, { ...simple, seed: randomSeed() }, [`regenerate`]);
    else submitting = true, apiFor(g.engine).post<Job>(`/v1/generations/${g.id}/rerun`, { new_seed: !exact }).then((j) => { const job = { ...j, engine: g.engine ?? conn.activeId ?? undefined }; jobs.byId[j.id] = job; addEntry(g.type, job, prompt, [exact ? 'same seed' : 'regenerate']); }).catch((e) => toastError(e, 'Could not start generation')).finally(() => (submitting = false));
  }

  function vary(g: Generation, strength: number) {
    const base = (g.simple ?? {}) as Record<string, any>;
    const simple = {
      ...imageSimpleParams(session.image, g.prompt ?? '', imageModel),
      ...base,
      model: base.model ?? g.model_id ?? session.image.model,
      prompt: g.prompt ?? '',
      reference_image: g.file_id,
      mask_image: null,
      denoise: strength,
      seed: randomSeed(),
      batch_size: 1,
      width: g.width,
      height: g.height,
      upscale: null,
    };
    void runMedia('image', g.prompt ?? '', simple, [`vary ${strength <= 0.4 ? 'subtle' : 'strong'}`], { [g.file_id]: g.engine });
  }

  async function editGen(g: Generation) {
    const s = g.simple as Record<string, any> | null;
    if (s && g.type === 'image') {
      session.mode = 'image';
      const p = session.image;
      if (s.model) p.model = s.model;
      p.negative_prompt = s.negative_prompt ?? '';
      p.steps = s.steps ?? p.steps;
      p.guidance = s.guidance ?? p.guidance;
      p.scheduler = s.scheduler ?? 'default';
      p.loras = s.loras ?? [];
      p.vae = s.vae ?? null;
      p.reference_image = s.reference_image ?? null;
      p.mask_image = s.mask_image ?? null;
      for (const f of [p.reference_image, p.mask_image]) if (f && g.engine) session.files[f] = g.engine;
      if (s.denoise) p.denoise = s.denoise;
      if (typeof s.seed === 'number') { p.seed = s.seed; p.random_seed = false; }
      const match = ASPECTS.find((a) => Math.abs(a.w / a.h - (s.width ?? 1) / (s.height ?? 1)) < 0.03);
      if (match) p.aspect = match.value;
    } else if (s && g.type === 'video') {
      session.mode = 'video';
      const p = session.video;
      if (s.model) p.model = s.model;
      p.negative_prompt = s.negative_prompt ?? null;
      p.frames = s.frames ?? p.frames;
      p.fps = s.fps ?? p.fps;
      p.steps = s.steps ?? p.steps;
      p.guidance = s.guidance ?? p.guidance;
      p.resolution = `${s.width}x${s.height}`;
      p.start_image = s.start_image ?? null;
      p.end_image = s.end_image ?? null;
      for (const f of [p.start_image, p.end_image]) if (f && g.engine) session.files[f] = g.engine;
      p.vmode = s.start_image ? 'i2v' : 't2v';
    } else {
      const full = await apiFor(g.engine).get<Generation>(`/v1/generations/${g.id}`);
      if (full.workflow) {
        session.graphs[g.type] = full.workflow;
        editor = g.type;
        toast('info', 'Opened the generation’s workflow in the node editor');
      }
      return;
    }
    session.draft = g.prompt ?? '';
    await tick();
    composerEl?.focus();
  }

  async function retryEntry(job: Job) {
    const simple = job.simple as Record<string, unknown> | null;
    const prompt = String((simple as any)?.prompt ?? job.title ?? '');
    // Inputs of a job live on the server that ran it.
    const homes = Object.fromEntries(FILE_INPUTS.map((k) => (simple as any)?.[k]).filter(Boolean).map((id) => [id, job.engine]));
    if (simple) void runMedia(job.kind, prompt, { ...simple, seed: randomSeed() }, ['retry'], homes);
    else {
      try {
        const j = await retryJob(job.id, false);
        addEntry(job.kind, j, prompt, ['retry']);
      } catch (e) {
        toastError(e, 'Could not retry');
      }
    }
  }

  // Honest time estimate for video, from this server's own completed runs of the same model.
  const videoEstimate = $derived.by(() => {
    const runs = Object.values(jobs.byId).filter((j) => j.kind === 'video' && j.status === 'completed' && j.result?.elapsed_s && (j.simple as any)?.model === session.video.model && (j.simple as any)?.steps
      && (!mediaEngine || (j.engine ?? conn.activeId) === mediaEngine));
    if (!runs.length) return null;
    const per = runs.slice(0, 5).map((j) => {
      const s = j.simple as any;
      return j.result!.elapsed_s / s.steps / ((s.width ?? 1) * (s.height ?? 1) * (s.frames ?? 1));
    });
    const k = per.reduce((a, b) => a + b, 0) / per.length;
    const [w, h] = (session.video.resolution || '832x480').split('x').map(Number);
    return `≈ ${duration(k * session.video.steps * w * h * session.video.frames)} on ${multi && mediaEngine ? profileName(mediaEngine) : 'this server'}`;
  });

  // ---------------------------------------------------------- node editor
  async function openEditor(kind: 'image' | 'video') {
    try {
      if (!session.graphs[kind]) {
        const text = session.draft;
        const simple = kind === 'image' ? imageSimpleParams(session.image, text, imageModel) : videoSimpleParams(session.video, text, videoModel);
        session.graphs[kind] = await switchToAdvanced(kind, null, simple, mediaEngine);
      }
      editor = kind;
    } catch (e) {
      toastError(e, 'Could not open the node editor');
    }
  }
  async function runWorkflow(kind: 'image' | 'video', g: Workflow) {
    // A graph names models directly; it runs on the server routed for this mode's model.
    const r = routeFor(kind, kind === 'image' ? session.image.model : session.video.model, session.route);
    if (!r.ok) throw new Error(r.error);
    const job = await submitJob({ kind, workflow: g, ...destFor(r.engine) }, r.engine);
    addEntry(kind, job, session.draft.trim() || 'Node workflow', ['workflow']);
    return job;
  }

  // ------------------------------------------------------------ chat settings
  let draftSystem = $state('');
  let draftParams = $state<ChatParams>({});
  $effect(() => {
    if (settingsOpen && isTextMode(mode)) {
      draftSystem = session.chatSystem;
      draftParams = { temperature: 0.7, top_p: 0.95, top_k: 40, min_p: 0.05, repeat_penalty: 1.05, max_tokens: 2048, ctx: chatModel?.default_ctx ?? 8192, gpu_layers: 999, ...(conv?.params ?? {}) };
    }
  });
  async function saveSettings() {
    try {
      const c = await ensureConv();
      session.chatSystem = draftSystem;
      const patch: Record<string, unknown> = { params: draftParams };
      if (mode === 'chat') patch.system_prompt = draftSystem;
      await capi.patch(`/v1/chat/conversations/${c.id}`, patch);
      c.params = { ...draftParams };
      if (mode === 'chat') c.system_prompt = draftSystem;
      toast('success', 'Chat settings applied');
      if (localLoaded && (llm?.ctx !== draftParams.ctx || llm?.gpu_layers !== draftParams.gpu_layers)) {
        toast('info', 'Context or GPU layers changed', 'The model will reload with the new settings on the next message.');
      }
    } catch (e) {
      toastError(e, 'Could not save settings');
    }
  }

  const where = $derived(multi && activeRoute?.ok ? `on ${profileName(activeRoute.engine)}${session.route ? '' : ' · auto'}` : null);
  const chips = $derived([
    ...(mode === 'code' ? ['tools run on this computer'] : summaryFor(mode, session, mode === 'image' ? imageModel : videoModel, { temperature: conv?.params?.temperature })),
    ...(where ? [where] : []),
  ]);
  const noModels = $derived(
    models.loaded && (isTextMode(mode) ? !models.chatModels.length : !byCategory(mode as 'image' | 'video').length),
  );
  const hero: Record<Mode, string> = {
    chat: 'Ask anything. Your model runs on your own GPU server.',
    code: 'Pick a project folder and describe the task. The agent reads, edits and runs code on this computer; your GPU server only provides the model.',
    image: 'Describe an image, or attach one to edit. It is generated on your GPU server.',
    video: 'Describe a scene to render as video on your GPU server. You can keep working while it renders.',
  };

  function keys(e: KeyboardEvent) {
    if (!focused || editor) return;
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
      const n = Number(e.key);
      if (n >= 1 && n <= 4) {
        e.preventDefault();
        setMode((['chat', 'code', 'image', 'video'] as Mode[])[n - 1]);
      }
    } else if (e.key === 'Escape' && settingsOpen) {
      settingsOpen = false;
      composerEl?.focus();
    }
  }
  $effect(() => {
    if (focused && active) tick().then(() => composerEl?.focus());
  });
</script>

<svelte:window onkeydown={keys} />

<div class="session" bind:clientWidth={width}>
  <div class="main" class:empty={empty && !settingsOpen}>
    <div class="scroll" bind:this={scroller} onscroll={onScroll}>
      <div class="inner" bind:this={innerEl}>
        {#if empty}
          <div class="hero">
            <h1>What shall we make?</h1>
            <p class="muted">{hero[mode]}</p>
            {#if noModels}
              <p class="warn">No {isTextMode(mode) ? 'chat' : mode} models are installed on {multi ? 'your servers' : 'this server'} yet.
                <button class="link" onclick={() => { prefs.view = 'models'; persistPrefs(); }}>Browse models</button></p>
            {/if}
          </div>
        {/if}

        {#each timeline as item (item.key)}
          {#if item.kind === 'message'}
            {@const m = msgById.get(item.id)}
            {#if m}
              <MessageView {m} sibs={siblings(msgs, m)} streaming={m.id === streamingId} modelName={chatModelByRef(m.model_ref, session.engine)?.name} engine={session.engine}
                onswitch={switchBranch} onregenerate={() => regenerate(m)} onedit={(t) => editMessage(m, t)} />
            {/if}
          {:else if item.kind === 'run'}
            {@const run = runsById.get(item.id)}
            {#if run}
              <RunView {run} isLast={run === ag.runs.at(-1)} running={agent.running} canContinue={agent.canContinue}
                onapprove={(id, ok) => agent.resolveApproval(id, ok)} oncontinue={() => agent.continueTask()}
                onchanges={(f) => { focusFile = f ?? ''; projPanel = true; }} />
            {/if}
          {:else}
            <div class="gen" data-mode={item.entry.mode}>
              <div class="gprompt">
                <span class="gmeta">{#if item.entry.mode === 'image'}<ImageIcon />{:else}<Clapperboard />{/if}{MODE_LABEL[item.entry.mode]}{#if item.entry.summary.length} · {item.entry.summary.slice(0, 3).join(' · ')}{/if}</span>
                <div class="bubble selectable">{item.entry.prompt}</div>
              </div>
              <GenCard entry={item.entry} canVary={imageModel?.capabilities?.modes?.includes('img2img') !== false} onrerun={rerun} onvary={vary} onedit={editGen} onretry={retryEntry} />
            </div>
          {/if}
        {/each}
        {#if statusText}<div class="status"><Spinner size={13} />{statusText}</div>{/if}
        {#if agent.error && mode === 'code'}<div class="agenterr" role="alert">{agent.error}</div>{/if}
      </div>
      {#if blocks.length && width >= 900}
        <button class="codebtn" onclick={() => (codeOverride = !codeOpen)} aria-pressed={codeOpen}><FileCode />{codeOpen ? 'Hide code' : `Code (${blocks.length})`}</button>
      {/if}
    </div>

    <div class="composer-wrap">
      <Composer {mode} bind:text={session.draft} bind:el={composerEl} bind:settingsOpen
        placeholder={mode === 'code' ? (ag.project ? 'Describe what to build or fix in this project…' : 'Choose a project folder, then describe the task…') : placeholderFor(mode, chatModel?.name)} {modelOptions} model={modelValue} onmodel={onModel}
        {chips} {attachments} {accept} {attachHint} {uploading} streaming={mode === 'code' ? agent.running : !!streamingId}
        websearch={mode === 'chat' ? { on: prefs.chatWebSearch, hint: prefs.chatWebSearch ? 'Web search is on: each message is searched first, with sources cited' : 'Search the web for each message and cite sources', toggle: () => { prefs.chatWebSearch = !prefs.chatWebSearch; persistPrefs(); } } : undefined}
        onmode={setMode} onsend={send} onstop={stop} onattach={attach} onremove={removeAttachment}>
        {#snippet agentbar()}
          {#if mode === 'code'}
            <div class="abar">
              <ProjectBar project={ag.project} permission={ag.permission} locked={agent.running}
                onproject={(p) => { ag.project = p; session.hasAgent = true; touch(session); }}
                onpermission={(m) => { ag.permission = m; }} />
              {#if ag.project}
                <button type="button" class="ptoggle" class:on={projPanel} aria-pressed={projPanel} onclick={() => (projPanel = !projPanel)} title="Project files, changes and diffs"><PanelRight />Project</button>
              {/if}
            </div>
            {#if waiting}<button type="button" class="needs" onclick={revealApproval}>⚠ Waiting for your approval: {waiting.approval?.title}. <u>Review</u></button>{/if}
            {#if supportHint}<p class="support" title={supportHint}>⚠ {supportHint}</p>{/if}
          {/if}
        {/snippet}
        {#snippet settings()}
          {#if mode === 'image'}
            <ServerChoice bind:route={session.route} result={mediaRoute} />
            <ImageSettings s={session.image} model={imageModel} engine={mediaEngine} files={session.files} {setQuality} />
            <div class="adv"><Button size="sm" variant="ghost" onclick={() => openEditor('image')}><Wand2 />Open node editor</Button><span class="faint small">Advanced: build the pipeline as a graph.</span></div>
          {:else if mode === 'video'}
            <ServerChoice bind:route={session.route} result={mediaRoute} />
            <VideoSettings s={session.video} model={videoModel} estimate={videoEstimate} engine={mediaEngine} files={session.files} />
            <div class="adv"><Button size="sm" variant="ghost" onclick={() => openEditor('video')}><Wand2 />Open node editor</Button><span class="faint small">Advanced: build the pipeline as a graph.</span></div>
          {:else}
            <div class="chatset">
              <ServerChoice bind:route={session.route} result={chatRoute} />
              {#if chatModel?.local}
                <div class="mrow">
                  {#if llm?.state === 'loading' && `local:${llm.model_id}` === modelRef}<span class="mstate"><Spinner size={12} />Loading model…</span>
                  {:else if localLoaded}<span class="mstate ok"><span class="dot"></span>Loaded{multi && chatEngine ? ` on ${profileName(chatEngine)}` : " on GPU"} · ctx {llm?.ctx}</span><Button size="sm" variant="ghost" onclick={unloadModel}><Power />Unload</Button>
                  {:else}<span class="mstate">Not loaded (loads on first message)</span><Button size="sm" variant="ghost" onclick={loadModel}><Power />Load now</Button>{/if}
                </div>
              {/if}
              {#if mode === 'chat'}
                <Field label="System prompt"><TextArea bind:value={draftSystem} minRows={2} maxRows={8} placeholder="You are a helpful assistant…" /></Field>
              {:else}
                <div class="codeset">
                  <p><strong>Local coding agent.</strong> The model runs on your server. Every file read, edit and command runs on <em>this computer</em>, inside the project folder you chose.</p>
                  <p class="faint small"><strong>{PERMISSION_LABEL[ag.permission]}:</strong> {PERMISSION_HINT[ag.permission]}</p>
                  <p class="faint small">Project: {ag.project ?? 'none selected'}{native.available ? '' : ' · the desktop app is required to run tools'}</p>
                </div>
              {/if}
              <Toggle checked={draftParams.thinking !== false} onchange={(v) => (draftParams.thinking = v)} label="Allow thinking" hint="Reasoning models think before answering (shown collapsed). Ignored by models without a thinking mode." />
              <div class="g2">
                <Slider bind:value={draftParams.temperature!} min={0} max={2} step={0.05} label="Temperature" format={(v) => v.toFixed(2)} />
                <Slider bind:value={draftParams.top_p!} min={0.05} max={1} step={0.01} label="Top P" format={(v) => v.toFixed(2)} />
                <Slider bind:value={draftParams.top_k!} min={0} max={200} label="Top K" />
                <Slider bind:value={draftParams.min_p!} min={0} max={0.5} step={0.01} label="Min P" format={(v) => v.toFixed(2)} />
                <Slider bind:value={draftParams.repeat_penalty!} min={1} max={1.5} step={0.01} label="Repeat penalty" format={(v) => v.toFixed(2)} />
                <Slider bind:value={draftParams.max_tokens!} min={64} max={16384} step={64} label="Max response tokens" />
              </div>
              {#if chatModel?.local}
                <div class="g2">
                  <Slider bind:value={draftParams.ctx!} min={2048} max={Math.min(chatModel.context_max ?? 32768, 131072)} step={1024} label="Context length" format={(v) => `${(v / 1024).toFixed(0)}K tokens`} />
                  <Slider bind:value={draftParams.gpu_layers!} min={0} max={999} label="GPU layers" format={(v) => (v >= 999 ? 'All' : String(v))} />
                </div>
              {/if}
              <div><Button size="sm" variant="primary" onclick={saveSettings}>Apply to this conversation</Button></div>
            </div>
          {/if}
        {/snippet}
      </Composer>
    </div>
  </div>

  {#if panelOpen && ag.project}
    <section class="codecol" aria-label="Project">
      <ProjectPanel project={ag.project} {focusFile} refreshKey={panelKey} onclose={() => (projPanel = false)} />
    </section>
  {:else if codeOpen}
    <section class="codecol" aria-label="Code from this conversation"><CodePanel {blocks} onattach={attach} /></section>
  {/if}
</div>

{#if editor}
  <WorkflowOverlay kind={editor} bind:graph={session.graphs[editor]} engine={mediaEngine} onclose={() => (editor = null)}
    onrun={(g) => runWorkflow(editor!, g)}
    onrebuild={async () => { const k = editor!; const t = session.draft; session.graphs[k] = await switchToAdvanced(k, null, k === 'image' ? imageSimpleParams(session.image, t, imageModel) : videoSimpleParams(session.video, t, videoModel), mediaEngine); }} />
{/if}

<style>
  .session { display: flex; height: 100%; min-height: 0; min-width: 0; }
  .main { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
  .scroll { position: relative; flex: 1; overflow-y: auto; min-height: 0; }
  .inner { max-width: 820px; margin: 0 auto; padding: 24px 20px 16px; display: flex; flex-direction: column; gap: 18px; }
  .main.empty { justify-content: center; }
  .main.empty .scroll { flex: 0 0 auto; overflow: visible; }
  .main.empty .inner { padding-bottom: 28px; }
  .hero { text-align: center; padding: 6vh 0 0; }
  .hero h1 { font-size: 28px; letter-spacing: -0.02em; margin-bottom: 8px; }
  .warn { margin-top: 14px; font-size: var(--fs-sm); color: var(--warn, var(--text-1)); }
  .link { border: 0; background: none; color: var(--accent-hi); cursor: pointer; font: inherit; text-decoration: underline; }
  .composer-wrap { padding: 0 20px 18px; flex-shrink: 0; }
  .main.empty .composer-wrap { padding-bottom: 6vh; }
  .status { display: flex; gap: 8px; align-items: center; color: var(--text-2); font-size: var(--fs-sm); padding-left: 40px; }
  .gen { display: flex; flex-direction: column; gap: 10px; }
  .gprompt { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
  .gmeta { display: inline-flex; align-items: center; gap: 5px; font-size: var(--fs-xs); color: var(--text-3); }
  .gmeta :global(svg) { width: 12px; height: 12px; color: var(--accent); }
  .bubble { max-width: 80%; padding: 9px 14px; border-radius: 18px 18px 4px 18px; background: var(--select-bg); border: 1px solid var(--select-line); white-space: pre-wrap; word-break: break-word; }
  .codebtn { position: sticky; float: right; top: 8px; margin: 8px 12px 0 0; display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-1); font-size: var(--fs-sm); cursor: pointer; z-index: 3; }
  .codebtn :global(svg) { width: 14px; height: 14px; color: var(--accent); }
  .codecol { flex: 0 0 44%; min-width: 320px; border-left: 1px solid var(--line-1); min-height: 0; }
  .abar { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .abar :global(.bar) { flex: 1; min-width: 0; }
  .ptoggle { display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 9px; margin-bottom: 4px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: var(--bg-0); color: var(--text-1); font-size: var(--fs-xs); cursor: pointer; flex-shrink: 0; }
  .ptoggle :global(svg) { width: 13px; height: 13px; color: var(--accent); }
  .ptoggle.on, .ptoggle:hover { background: var(--select-bg); }
  .needs { display: block; width: 100%; margin: 0 0 6px; padding: 7px 12px; border-radius: var(--r-md); border: 1px solid var(--warn); background: var(--warn-bg); color: var(--text-0); font-size: var(--fs-sm); text-align: left; cursor: pointer; }
  .support { margin: 0 4px 4px; font-size: var(--fs-xs); color: var(--text-2); }
  .agenterr { padding: 10px 14px; border-radius: var(--r-lg); border: 1px solid var(--warn); background: var(--warn-bg); font-size: var(--fs-sm); }
  .codeset p { margin: 0 0 8px; font-size: var(--fs-sm); }
  .adv { display: flex; align-items: center; gap: 10px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line-0); }
  .small { font-size: var(--fs-xs); }
  .chatset { display: flex; flex-direction: column; gap: 14px; }
  .g2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
  .mrow { display: flex; align-items: center; gap: 8px; }
  .mstate { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 10px; border-radius: var(--r-full); border: 1px solid var(--line-1); background: var(--bg-2); color: var(--text-2); font-size: var(--fs-xs); }
  .mstate.ok { color: var(--ok); border-color: rgba(95, 211, 155, 0.25); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); }
</style>
