<script lang="ts">
  import { Background, BackgroundVariant, Controls, MiniMap, Panel, SvelteFlow, useSvelteFlow, type Connection } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import { CircleCheck, Copy, FileDown, FileUp, FolderOpen, Group, Maximize, Play, Plus, Redo2, RotateCcw, Save, ShieldCheck, Square, Star, Trash2, Undo2, Ungroup, Type } from '@lucide/svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { open as openDialog, save as saveDialog } from '@tauri-apps/plugin-dialog';
  import { onDestroy, tick, untrack } from 'svelte';
  import { apiFor, qs } from '../api/client';
  import { isTauri, mediaUrl } from '../api/transport';
  import type { Job, NodeSpec, SavedWorkflow, ValidationIssue, Workflow } from '../api/types';
  import { cancelJob, jobs, onGeneration } from '../stores/jobs.svelte';
  import { loadNodeLibrary, models } from '../stores/models.svelte';
  import { toast, toastError } from '../stores/toast.svelte';
  import Button from '../ui/Button.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Menu, { type MenuItem } from '../ui/Menu.svelte';
  import Modal from '../ui/Modal.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { confirmDialog, promptDialog } from '../ui/dialogs.svelte';
  import { ago } from '../util/format';
  import { setEditor } from './context';
  import { cloneSelection, connectionError, edgeColor, fromFlow, specMap, toFlow, uid, defaultsFor, inputSockets, compatible, type FlowEdge, type FlowNode } from './graph';
  import { stripUi } from './modes';
  import GroupNode from './GroupNode.svelte';
  import NodePalette from './NodePalette.svelte';
  import RerouteNode from './RerouteNode.svelte';
  import SarnessNode from './SarnessNode.svelte';

  interface Props {
    kind: 'image' | 'video';
    graph: Workflow | null;
    active: boolean;
    onrun: (g: Workflow) => Promise<Job>;
    onrebuild: () => Promise<void>;
    /** Server whose node library, models and saved workflows the editor uses (null = primary). */
    engine?: string | null;
  }
  let { kind, graph = $bindable(), active, onrun, onrebuild, engine = null }: Props = $props();
  const api = $derived(apiFor(engine));

  const flow = useSvelteFlow();
  const nodeTypes = { sarness: SarnessNode, reroute: RerouteNode, group: GroupNode } as any;

  let nodes = $state.raw<FlowNode[]>([]);
  let edges = $state.raw<FlowEdge[]>([]);
  let name = $state('Untitled workflow');
  let savedId = $state<string | null>(null);
  let lastEmitted = '';
  let ready = $state(false);
  let jobId = $state<string | null>(null);
  let issues = $state<ValidationIssue[]>([]);
  let validating = $state(false);
  let running = $state(false);
  let outputs = $state<Record<string, string>>({});
  let palette = $state<{ x: number; y: number; flow: { x: number; y: number }; accept: string | null; from: { node: string; handle: string } | null } | null>(null);
  let menu = $state({ open: false, x: 0, y: 0, items: [] as MenuItem[] });
  let openList = $state<SavedWorkflow[] | null>(null);
  let clipboard: { nodes: FlowNode[]; edges: FlowEdge[] } | null = null;

  const lib = $derived(models.nodes);
  const specs = $derived(specMap(lib));
  const job = $derived(jobId ? jobs.byId[jobId] : null);
  const nodeStates = $derived(jobId ? (jobs.nodeStates[jobId] ?? job?.node_states ?? {}) : {});
  const liveProg = $derived(jobId ? jobs.progress[jobId] : null);
  const jobActive = $derived(!!job && (job.status === 'queued' || job.status === 'running'));

  // ------------------------------------------------------------ load / sync
  $effect(() => {
    loadNodeLibrary(false, engine).catch((e) => toastError(e, 'Could not load the node library'));
  });

  $effect(() => {
    const g = graph;
    const l = lib;
    if (!l) return;
    const json = g ? JSON.stringify(g) : '';
    if (json === lastEmitted) return;
    untrack(() => load(g));
  });

  function load(g: Workflow | null) {
    if (!lib) return;
    if (!g) {
      nodes = [];
      edges = [];
      name = 'Untitled workflow';
      lastEmitted = '';
      ready = true;
      return;
    }
    const f = toFlow(g, specs, lib);
    nodes = f.nodes;
    edges = f.edges;
    name = g.name ?? 'Untitled workflow';
    savedId = (g.ui as any)?.saved_id ?? null;
    lastEmitted = JSON.stringify(g);
    undoStack = [];
    redoStack = [];
    ready = true;
    tick().then(() => setTimeout(() => flow.fitView({ padding: 0.15, duration: 0 }), 30));
  }

  let commitTimer: ReturnType<typeof setTimeout> | null = null;
  function commit() {
    if (commitTimer) clearTimeout(commitTimer);
    commitTimer = setTimeout(() => {
      const g = fromFlow(nodes, edges, { name, kind, ui: { ...(savedId ? { saved_id: savedId } : {}) } as any });
      lastEmitted = JSON.stringify(g);
      graph = g;
    }, 250);
  }
  $effect(() => {
    void nodes;
    void edges;
    void name;
    if (ready) untrack(commit);
  });

  $effect(() => {
    if (active && ready) tick().then(() => setTimeout(() => flow.fitView({ padding: 0.15, duration: 0 }), 40));
  });

  // ------------------------------------------------------------- undo/redo
  let undoStack: string[] = [];
  let redoStack: string[] = [];
  let lastSnap = 0;
  function snapshot(coalesce = false) {
    const now = Date.now();
    if (coalesce && now - lastSnap < 800) return;
    lastSnap = now;
    undoStack.push(JSON.stringify({ nodes, edges }));
    if (undoStack.length > 120) undoStack.shift();
    redoStack = [];
  }
  function restore(s: string) {
    const v = JSON.parse(s);
    nodes = v.nodes;
    edges = v.edges;
  }
  function undo() {
    const s = undoStack.pop();
    if (!s) return;
    redoStack.push(JSON.stringify({ nodes, edges }));
    restore(s);
  }
  function redo() {
    const s = redoStack.pop();
    if (!s) return;
    undoStack.push(JSON.stringify({ nodes, edges }));
    restore(s);
  }

  // ---------------------------------------------------------------- editing
  function setParam(id: string, key: string, value: unknown) {
    snapshot(true);
    nodes = nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, params: { ...(n.data.params ?? {}), [key]: value } } } : n));
  }

  async function rename(id: string) {
    const n = nodes.find((x) => x.id === id);
    if (!n) return;
    const spec = specs.get(n.data.nodeType ?? '');
    const v = await promptDialog(n.type === 'group' ? 'Rename group' : 'Rename node', n.data.label ?? spec?.title ?? '', { confirmLabel: 'Rename' });
    if (v === null) return;
    snapshot();
    nodes = nodes.map((x) => (x.id === id ? { ...x, data: { ...x.data, label: v } } : x));
  }

  function addNode(spec: NodeSpec, at: { x: number; y: number }, from?: { node: string; handle: string } | null) {
    snapshot();
    const id = uid('n');
    const n: FlowNode = {
      id, type: spec.passthrough ? 'reroute' : 'sarness', position: at,
      data: { nodeType: spec.type, params: defaultsFor(spec) },
    };
    let newEdges = edges;
    if (from) {
      const fromNode = nodes.find((x) => x.id === from.node);
      const fromSpec = specs.get(fromNode?.data.nodeType ?? '');
      const outT = fromSpec?.outputs.find((o) => o.name === from.handle)?.type ?? 'ANY';
      const target = inputSockets(spec).find((s) => compatible(outT, s.type));
      if (target) {
        newEdges = [...edges, mkEdge(from.node, from.handle, id, target.name, outT)];
      }
    }
    nodes = [...nodes.map((x) => ({ ...x, selected: false })), { ...n, selected: true }];
    edges = newEdges;
  }

  function mkEdge(source: string, sourceHandle: string, target: string, targetHandle: string, type: string): FlowEdge {
    return { id: uid('e'), source, sourceHandle, target, targetHandle, data: { type }, style: `stroke:${edgeColor(type, lib)};` };
  }

  function isValidConnection(c: Connection | any) {
    return connectionError(c, nodes, edges, specs) === null;
  }

  function onconnect(c: Connection) {
    const n = nodes.find((x) => x.id === c.source);
    const spec = specs.get(n?.data.nodeType ?? '');
    let type = spec?.outputs.find((o) => o.name === c.sourceHandle)?.type ?? 'ANY';
    if (spec?.passthrough) {
      const inc = edges.find((e) => e.target === c.source);
      type = (inc?.data?.type as string) ?? 'ANY';
    }
    // Svelte Flow has already appended its own unstyled edge (id "xy-edge__…");
    // replace it, and any existing edge into the same input (one per socket).
    const before = edges.filter((e) => !e.id.startsWith('xy-edge'));
    undoStack.push(JSON.stringify({ nodes, edges: before }));
    redoStack = [];
    const kept = before.filter((e) => !(e.target === c.target && e.targetHandle === c.targetHandle));
    edges = [...kept, mkEdge(c.source, c.sourceHandle!, c.target, c.targetHandle!, type)];
  }

  function onconnectend(event: MouseEvent | TouchEvent, state: any) {
    if (state?.isValid || !state?.fromNode || state?.fromHandle?.type !== 'source') return;
    const pt = 'changedTouches' in event ? event.changedTouches[0] : event;
    const fromSpec = specs.get((state.fromNode.data as any)?.nodeType ?? '');
    const outT = fromSpec?.outputs.find((o) => o.name === state.fromHandle.id)?.type ?? null;
    palette = {
      x: pt.clientX, y: pt.clientY, flow: flow.screenToFlowPosition({ x: pt.clientX, y: pt.clientY }),
      accept: outT, from: { node: state.fromNode.id, handle: state.fromHandle.id },
    };
  }

  function openPalette(clientX: number, clientY: number) {
    palette = { x: clientX, y: clientY, flow: flow.screenToFlowPosition({ x: clientX, y: clientY }), accept: null, from: null };
  }

  function selection() {
    return { sn: nodes.filter((n) => n.selected), se: edges.filter((e) => e.selected) };
  }

  function copy() {
    const { sn } = selection();
    if (!sn.length) return;
    const ids = new Set(sn.map((n) => n.id));
    // Include children of selected groups.
    const all = nodes.filter((n) => ids.has(n.id) || (n.parentId && ids.has(n.parentId)));
    clipboard = { nodes: structuredClone(all), edges: structuredClone(edges.filter((e) => ids.has(e.source) && ids.has(e.target))) };
    try {
      navigator.clipboard.writeText(JSON.stringify(fromFlow(clipboard.nodes, clipboard.edges, { name: 'clipboard', kind })));
    } catch {
      /* clipboard may be unavailable */
    }
  }

  function paste() {
    if (!clipboard) return;
    snapshot();
    const c = cloneSelection(clipboard.nodes, clipboard.edges);
    nodes = [...nodes.map((n) => ({ ...n, selected: false })), ...c.nodes];
    edges = [...edges, ...c.edges];
    clipboard = { nodes: c.nodes.map((n) => ({ ...n })), edges: c.edges };
  }

  function duplicate() {
    copy();
    paste();
  }

  function remove() {
    const { sn, se } = selection();
    if (!sn.length && !se.length) return;
    snapshot();
    const ids = new Set(sn.map((n) => n.id));
    // Deleting a group keeps its children (ungroups them first).
    nodes = nodes.filter((n) => !ids.has(n.id)).map((n) => (n.parentId && ids.has(n.parentId) ? absolutize(n, sn.find((g) => g.id === n.parentId)!) : n));
    edges = edges.filter((e) => !e.selected && !ids.has(e.source) && !ids.has(e.target));
  }

  function absolutize(n: FlowNode, parent: FlowNode): FlowNode {
    const { parentId, ...rest } = n;
    return { ...rest, position: { x: parent.position.x + n.position.x, y: parent.position.y + n.position.y } } as FlowNode;
  }

  function group() {
    const sel = nodes.filter((n) => n.selected && n.type !== 'group' && !n.parentId);
    if (sel.length < 1) return toast('info', 'Select nodes to group');
    snapshot();
    const xs = sel.map((n) => n.position.x), ys = sel.map((n) => n.position.y);
    const ws = sel.map((n) => (n as any).measured?.width ?? 280), hs = sel.map((n) => (n as any).measured?.height ?? 160);
    const x0 = Math.min(...xs) - 30, y0 = Math.min(...ys) - 54;
    const x1 = Math.max(...xs.map((x, i) => x + ws[i])) + 30, y1 = Math.max(...ys.map((y, i) => y + hs[i])) + 30;
    const gid = uid('g');
    const g: FlowNode = { id: gid, type: 'group', position: { x: x0, y: y0 }, data: { label: 'Group' }, width: x1 - x0, height: y1 - y0, style: `width:${x1 - x0}px;height:${y1 - y0}px;`, zIndex: -1 };
    const selIds = new Set(sel.map((n) => n.id));
    nodes = [g, ...nodes.map((n) => (selIds.has(n.id) ? { ...n, parentId: gid, selected: false, position: { x: n.position.x - x0, y: n.position.y - y0 } } : n))];
  }

  function ungroup(gid: string) {
    const g = nodes.find((n) => n.id === gid);
    if (!g) return;
    snapshot();
    nodes = nodes.filter((n) => n.id !== gid).map((n) => (n.parentId === gid ? absolutize(n, g) : n));
  }

  function insertReroute(edgeId: string, clientX: number, clientY: number) {
    const e = edges.find((x) => x.id === edgeId);
    const spec = specs.get('util.reroute');
    if (!e || !spec) return;
    snapshot();
    const id = uid('r');
    const p = flow.screenToFlowPosition({ x: clientX, y: clientY });
    const t = (e.data?.type as string) ?? 'ANY';
    nodes = [...nodes, { id, type: 'reroute', position: { x: p.x - 8, y: p.y - 8 }, data: { nodeType: 'util.reroute', params: {} } }];
    edges = [...edges.filter((x) => x.id !== edgeId), mkEdge(e.source, e.sourceHandle, id, 'in', t), mkEdge(id, 'out', e.target, e.targetHandle, t)];
  }

  // ----------------------------------------------------------- context menus
  function nodeMenu({ node, event }: { node: any; event: MouseEvent }) {
    event.preventDefault();
    if (!node.selected) nodes = nodes.map((n) => ({ ...n, selected: n.id === node.id }));
    const items: MenuItem[] = node.type === 'group'
      ? [
          { label: 'Rename group', icon: Type, run: () => rename(node.id) },
          { label: 'Ungroup', icon: Ungroup, run: () => ungroup(node.id) },
          { separator: true },
          { label: 'Delete group (keep nodes)', icon: Trash2, danger: true, run: remove },
        ]
      : [
          { label: 'Rename', icon: Type, run: () => rename(node.id) },
          { label: 'Duplicate', icon: Copy, hint: 'Ctrl+D', run: duplicate },
          { label: 'Copy', icon: Copy, hint: 'Ctrl+C', run: copy },
          { label: 'Group selection', icon: Group, hint: 'Ctrl+G', run: group },
          ...(node.parentId ? [{ label: 'Remove from group', icon: Ungroup, run: () => { snapshot(); const g = nodes.find((x) => x.id === node.parentId)!; nodes = nodes.map((n) => (n.id === node.id ? absolutize(n, g) : n)); } }] : []),
          { separator: true },
          { label: 'Delete', icon: Trash2, danger: true, hint: 'Del', run: remove },
        ];
    menu = { open: true, x: event.clientX, y: event.clientY, items };
  }

  function paneMenu({ event }: { event: MouseEvent }) {
    event.preventDefault();
    menu = {
      open: true, x: event.clientX, y: event.clientY, items: [
        { label: 'Add node…', icon: Plus, hint: 'Space', run: () => openPalette(event.clientX, event.clientY) },
        { label: 'Paste', icon: Copy, hint: 'Ctrl+V', disabled: !clipboard, run: paste },
        { label: 'Select all', run: () => (nodes = nodes.map((n) => ({ ...n, selected: true }))) },
        { label: 'Fit view', icon: Maximize, run: () => flow.fitView({ padding: 0.15, duration: 200 }) },
      ],
    };
  }

  // ---------------------------------------------------------- run & validate
  function engineGraph(): Workflow {
    return stripUi(fromFlow(nodes, edges, { name, kind }));
  }

  async function validate(silent = false) {
    validating = true;
    try {
      const r = await api.post<{ valid: boolean; issues: ValidationIssue[] }>('/v1/workflows/validate', { workflow: engineGraph() });
      issues = r.issues;
      if (!silent) {
        if (r.valid) toast('success', 'Workflow is valid');
        else toast('warning', `${r.issues.length} issue${r.issues.length > 1 ? 's' : ''} found`, r.issues[0]?.message);
      }
      return r.valid;
    } catch (e) {
      toastError(e, 'Validation failed');
      return false;
    } finally {
      validating = false;
    }
  }

  async function run() {
    if (!(await validate(true))) {
      toast('warning', 'Fix the highlighted issues before running', issues[0]?.message);
      return;
    }
    running = true;
    outputs = {};
    try {
      const j = await onrun(engineGraph());
      jobId = j.id;
    } catch {
      /* toast shown by caller */
    } finally {
      running = false;
    }
  }

  const unsubGen = onGeneration((g) => {
    if (g.job_id && g.job_id === jobId) {
      const nid = (g.metadata as any)?.node_id;
      if (nid) outputs = { ...outputs, [nid]: mediaUrl(g.thumb_url, g.engine) };
    }
  });
  onDestroy(() => unsubGen());

  // ------------------------------------------------------------- persistence
  async function saveToEngine(asPreset = false) {
    const g = engineGraph();
    try {
      if (savedId && !asPreset) {
        await api.put(`/v1/workflows/${savedId}`, { name, graph: g });
        toast('success', 'Workflow saved', name);
      } else {
        const label = await promptDialog(asPreset ? 'Save as preset' : 'Save workflow', name, { confirmLabel: 'Save' });
        if (!label) return;
        name = label;
        const row = await api.post<SavedWorkflow>('/v1/workflows', { name: label, kind, graph: { ...g, name: label }, is_preset: asPreset });
        if (!asPreset) savedId = row.id;
        toast('success', asPreset ? 'Preset saved' : 'Workflow saved', label);
      }
      commit();
    } catch (e) {
      toastError(e, 'Save failed');
    }
  }

  async function showOpen() {
    try {
      openList = await api.get<SavedWorkflow[]>(`/v1/workflows${qs({ kind })}`);
    } catch (e) {
      toastError(e);
    }
  }

  async function openSaved(w: SavedWorkflow) {
    const full = await api.get<SavedWorkflow>(`/v1/workflows/${w.id}`);
    openList = null;
    const g = { ...full.graph!, ui: { ...(full.graph!.ui ?? {}), ...(w.is_preset ? {} : { saved_id: w.id }) } } as Workflow;
    graph = g;
    load(g);
  }

  async function deleteSaved(w: SavedWorkflow) {
    if (!(await confirmDialog(`Delete “${w.name}”?`, undefined, { danger: true, confirmLabel: 'Delete' }))) return;
    await api.del(`/v1/workflows/${w.id}`);
    openList = openList?.filter((x) => x.id !== w.id) ?? null;
    if (savedId === w.id) savedId = null;
  }

  async function exportFile() {
    const text = JSON.stringify(fromFlow(nodes, edges, { name, kind }), null, 2);
    const file = `${name.replace(/[^\w.-]+/g, '-').toLowerCase() || 'workflow'}.sarness.json`;
    if (isTauri) {
      const dest = await saveDialog({ defaultPath: file, filters: [{ name: 'Sarness workflow', extensions: ['json'] }] });
      if (!dest) return;
      await invoke('write_text_file', { dest, contents: text });
      toast('success', 'Workflow exported', dest);
    } else {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      a.download = file;
      a.click();
    }
  }

  async function importFile() {
    try {
      let text: string | null = null;
      if (isTauri) {
        const path = await openDialog({ multiple: false, filters: [{ name: 'Sarness workflow', extensions: ['json'] }] });
        if (!path) return;
        text = await invoke<string>('read_text_file', { path });
      } else {
        text = await new Promise<string | null>((res) => {
          const i = document.createElement('input');
          i.type = 'file';
          i.accept = '.json,application/json';
          i.onchange = async () => res(i.files?.[0] ? await i.files[0].text() : null);
          i.click();
        });
      }
      if (!text) return;
      const g = JSON.parse(text) as Workflow;
      if (g.format !== 'sarness.workflow' || !Array.isArray(g.nodes)) throw new Error('This file is not a Sarness workflow');
      if (g.kind && g.kind !== kind) toast('warning', `This is a ${g.kind} workflow; opening it in the ${kind} editor`);
      snapshot();
      savedId = null;
      graph = { ...g, ui: {} };
      load(graph);
      toast('success', 'Workflow imported', g.name);
    } catch (e) {
      toastError(e, 'Import failed');
    }
  }

  async function rebuild() {
    if (nodes.length && !(await confirmDialog('Rebuild from simple settings?', 'This replaces the current graph with the workflow generated from your simple-mode settings.', { confirmLabel: 'Rebuild' }))) return;
    snapshot();
    savedId = null;
    await onrebuild();
  }

  // ------------------------------------------------------------- keyboard
  function keys(e: KeyboardEvent) {
    if (!active) return;
    const t = e.target as HTMLElement;
    if (t?.closest('input, textarea, select, [contenteditable]')) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();
    if (mod && k === 'z' && !e.shiftKey) { undo(); e.preventDefault(); }
    else if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { redo(); e.preventDefault(); }
    else if (mod && k === 'c') { copy(); e.preventDefault(); }
    else if (mod && k === 'v') { paste(); e.preventDefault(); }
    else if (mod && k === 'd') { duplicate(); e.preventDefault(); }
    else if (mod && k === 'g') { group(); e.preventDefault(); }
    else if (mod && k === 'a') { nodes = nodes.map((n) => ({ ...n, selected: true })); e.preventDefault(); }
    else if (mod && k === 's') { saveToEngine(); e.preventDefault(); }
    else if (mod && k === 'enter') { run(); e.preventDefault(); }
    else if (!mod && (e.key === ' ' || e.key === 'Tab') && !palette) {
      e.preventDefault();
      const r = (document.querySelector('.wf-canvas') as HTMLElement)?.getBoundingClientRect();
      openPalette(r ? r.left + r.width / 2 - 190 : 300, r ? r.top + r.height / 3 : 200);
    } else if (e.key === 'Delete' || e.key === 'Backspace') { remove(); e.preventDefault(); }
  }

  // --------------------------------------------------------------- context
  setEditor({
    get specs() { return specs; },
    get lib() { return lib; },
    kind,
    state: (id) => (nodeStates as any)[id] ?? null,
    progress: (id) => (liveProg && liveProg.node_id === id && liveProg.total ? { value: (liveProg.step ?? 0) / liveProg.total, text: liveProg.text } : null),
    preview: (id) => (jobId && liveProg?.node_id === id ? jobs.previews[jobId] ?? null : null),
    output: (id) => outputs[id] ?? null,
    issue: (id) => issues.find((i) => i.node_id === id)?.message ?? null,
    connected: (id, handle) => edges.some((e) => e.target === id && e.targetHandle === handle),
    setParam,
    rename,
    socketColor: (t) => edgeColor(t, lib),
  });

  const graphIssues = $derived(issues.filter((i) => !i.node_id));
</script>

<svelte:window onkeydown={keys} />

<div class="editor">
  {#if !lib}
    <div class="loading"><Spinner size={22} /> Loading node library…</div>
  {:else}
    <div class="wf-canvas">
      <SvelteFlow bind:nodes bind:edges {nodeTypes} colorMode="dark" {isValidConnection} {onconnect} {onconnectend}
        deleteKey={null} minZoom={0.15} maxZoom={2.5} snapGrid={[10, 10]}
        defaultEdgeOptions={{ type: 'default' }} connectionRadius={28} proOptions={{ hideAttribution: true }}
        onnodedragstart={() => snapshot()} onnodecontextmenu={nodeMenu} onpanecontextmenu={paneMenu}
        onedgeclick={({ edge, event }) => event.detail === 2 && insertReroute(edge.id, event.clientX, event.clientY)}
        onpaneclick={({ event }) => event.detail === 2 && openPalette(event.clientX, event.clientY)}
        onselectionstart={() => (menu.open = false)}>
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} bgColor="var(--bg-0)" patternColor="rgba(var(--fg-rgb),0.09)" />
        <Controls showLock={false} position="bottom-left" />
        <MiniMap position="bottom-right" pannable zoomable bgColor="var(--bg-1)" maskColor="var(--scrim)" nodeColor="var(--bg-5)" nodeStrokeColor="var(--line-2)" />

        <Panel position="top-left">
          <div class="toolbar glass">
            <input class="name" bind:value={name} spellcheck="false" aria-label="Workflow name" />
            <span class="sep"></span>
            <IconButton label="Add node  (Space)" onclick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); openPalette(r.left, r.bottom + 8); }}><Plus /></IconButton>
            <IconButton label="Undo  (Ctrl+Z)" onclick={undo}><Undo2 /></IconButton>
            <IconButton label="Redo  (Ctrl+Shift+Z)" onclick={redo}><Redo2 /></IconButton>
            <IconButton label="Group selection  (Ctrl+G)" onclick={group}><Group /></IconButton>
            <IconButton label="Fit view" onclick={() => flow.fitView({ padding: 0.15, duration: 220 })}><Maximize /></IconButton>
            <span class="sep"></span>
            <IconButton label="Open saved workflow or preset" onclick={showOpen}><FolderOpen /></IconButton>
            <IconButton label="Save  (Ctrl+S)" onclick={() => saveToEngine(false)}><Save /></IconButton>
            <IconButton label="Save as preset" onclick={() => saveToEngine(true)}><Star /></IconButton>
            <IconButton label="Import workflow file" onclick={importFile}><FileUp /></IconButton>
            <IconButton label="Export workflow file" onclick={exportFile}><FileDown /></IconButton>
            <IconButton label="Rebuild from simple settings" onclick={rebuild}><RotateCcw /></IconButton>
          </div>
        </Panel>

        <Panel position="top-right">
          <div class="runbar glass">
            {#if job}
              <div class="jstate">
                {#if jobActive}<Spinner size={13} />{:else if job.status === 'completed'}<CircleCheck />{/if}
                <span class="truncate">{jobActive ? (liveProg?.text ?? job.progress_text ?? job.status) : job.status === 'failed' ? (job.error?.message ?? 'Failed') : job.status}</span>
                {#if jobActive}<span class="tnum faint">{Math.round((liveProg?.progress ?? job.progress ?? 0) * 100)}%</span>{/if}
              </div>
            {/if}
            <Button size="sm" variant="ghost" onclick={() => validate()} loading={validating}><ShieldCheck />Validate</Button>
            {#if jobActive}
              <Button size="sm" variant="danger" onclick={() => jobId && cancelJob(jobId)}><Square />Cancel</Button>
            {/if}
            <Button size="sm" variant="primary" onclick={run} loading={running}><Play />Run</Button>
          </div>
        </Panel>

        {#if graphIssues.length}
          <Panel position="bottom-center">
            <div class="issues glass">
              {#each graphIssues as i (i.message)}<div>{i.message}</div>{/each}
            </div>
          </Panel>
        {/if}

        {#if !nodes.length}
          <Panel position="top-center">
            <div class="hint glass">Press <kbd>Space</kbd> or double-click the canvas to add nodes, or rebuild from your simple settings.</div>
          </Panel>
        {/if}
      </SvelteFlow>
    </div>
  {/if}
</div>

{#if palette && lib}
  <NodePalette {lib} x={palette.x} y={palette.y} acceptType={palette.accept}
    onpick={(s) => { addNode(s, palette!.flow, palette!.from); palette = null; }}
    onclose={() => (palette = null)} />
{/if}

<Menu bind:open={menu.open} bind:x={menu.x} bind:y={menu.y} bind:items={menu.items} />

{#if openList}
  <Modal open={true} title="Open workflow" subtitle={`Saved ${kind} workflows and presets on this server`} onclose={() => (openList = null)} width={560}>
    {#if !openList.length}
      <p class="muted">Nothing saved yet. Use Save or Save as preset in the toolbar.</p>
    {:else}
      <div class="saved">
        {#each openList as w (w.id)}
          <div class="srow">
            <button class="sopen" onclick={() => openSaved(w)}>
              {#if w.is_preset}<Star />{:else}<FolderOpen />{/if}
              <span class="truncate">{w.name}</span>
              <span class="faint">{ago(w.updated_at)}</span>
            </button>
            <IconButton label="Delete" tone="danger" size="sm" onclick={() => deleteSaved(w)}><Trash2 /></IconButton>
          </div>
        {/each}
      </div>
    {/if}
  </Modal>
{/if}

<style>
  .editor { flex: 1; min-width: 0; min-height: 0; display: flex; position: relative; }
  .wf-canvas { flex: 1; min-width: 0; }
  .loading { margin: auto; display: flex; gap: 10px; align-items: center; color: var(--text-2); }
  .toolbar, .runbar {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 5px;
    border-radius: var(--r-lg);
    background: var(--glass-strong);
    box-shadow: var(--shadow-2);
  }
  .name { width: 190px; height: 28px; padding: 0 8px; border: 1px solid transparent; border-radius: var(--r-sm); background: transparent; color: var(--text-0); font-weight: 600; outline: none; }
  .name:hover { border-color: var(--line-1); }
  .name:focus { border-color: var(--select-line); background: var(--bg-0); }
  .sep { width: 1px; height: 18px; margin: 0 4px; background: var(--line-1); }
  .runbar { gap: 6px; padding: 5px 5px 5px 10px; max-width: 560px; }
  .jstate { display: flex; align-items: center; gap: 7px; font-size: var(--fs-sm); color: var(--text-1); max-width: 260px; min-width: 0; }
  .jstate :global(svg) { width: 14px; height: 14px; color: var(--ok); flex-shrink: 0; }
  .issues { padding: 10px 14px; border-radius: var(--r-md); color: var(--err); font-size: var(--fs-sm); background: var(--glass-strong); }
  .hint { padding: 9px 14px; border-radius: var(--r-md); color: var(--text-2); font-size: var(--fs-sm); background: var(--glass-strong); }
  kbd { padding: 1px 5px; border-radius: 4px; background: var(--bg-5); color: var(--text-0); font-size: 11px; }
  .saved { display: flex; flex-direction: column; gap: 4px; }
  .srow { display: flex; align-items: center; gap: 6px; }
  .sopen { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: var(--r-md); border: 1px solid var(--line-0); background: var(--bg-1); color: var(--text-0); cursor: pointer; text-align: left; }
  .sopen:hover { border-color: var(--select-line); background: var(--select-bg); }
  .sopen :global(svg) { width: 15px; height: 15px; color: var(--lavender); flex-shrink: 0; }
  .sopen .truncate { flex: 1; }

  /* Svelte Flow theming — Sarness Obsidian */
  .wf-canvas :global(.svelte-flow) { --xy-background-color: var(--bg-0); --xy-edge-stroke-width: 2; --xy-connectionline-stroke-default: var(--accent-hi); --xy-connectionline-stroke-width: 2; --xy-selection-background-color: rgba(var(--accent-rgb), 0.08); --xy-selection-border: 1px solid rgba(var(--accent-rgb), 0.4); }
  .wf-canvas :global(.svelte-flow__node) { border-radius: 12px; }
  .wf-canvas :global(.svelte-flow__node-sarness), .wf-canvas :global(.svelte-flow__node-reroute) { background: none; border: 0; padding: 0; box-shadow: none; width: auto; }
  .wf-canvas :global(.svelte-flow__node-group) { background: none; border: 0; padding: 0; }
  .wf-canvas :global(.svelte-flow__edge-path) { stroke-width: 2.2; opacity: 0.85; }
  .wf-canvas :global(.svelte-flow__edge.selected .svelte-flow__edge-path) { stroke-width: 3.2; opacity: 1; filter: drop-shadow(0 0 4px rgba(var(--accent-rgb), 0.7)); }
  .wf-canvas :global(.svelte-flow__controls) { border-radius: var(--r-md); overflow: hidden; box-shadow: var(--shadow-2); border: 1px solid var(--line-1); }
  .wf-canvas :global(.svelte-flow__controls-button) { background: var(--bg-3); border-bottom: 1px solid var(--line-0); color: var(--text-1); fill: var(--text-1); }
  .wf-canvas :global(.svelte-flow__controls-button:hover) { background: var(--bg-5); }
  .wf-canvas :global(.svelte-flow__minimap) { border-radius: var(--r-md); overflow: hidden; border: 1px solid var(--line-1); }
  .wf-canvas :global(.svelte-flow__panel) { margin: 12px; }
</style>
