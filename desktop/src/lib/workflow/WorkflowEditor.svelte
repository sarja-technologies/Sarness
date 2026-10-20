<script lang="ts">
  import { SvelteFlowProvider } from '@xyflow/svelte';
  import type { Job, Workflow } from '../api/types';
  import EditorInner from './EditorInner.svelte';

  interface Props {
    kind: 'image' | 'video';
    graph: Workflow | null;
    active: boolean;
    onrun: (g: Workflow) => Promise<Job>;
    onrebuild: () => Promise<void>;
    engine?: string | null;
  }
  let { kind, graph = $bindable(), active, onrun, onrebuild, engine = null }: Props = $props();
</script>

<SvelteFlowProvider>
  <EditorInner {kind} bind:graph {active} {onrun} {onrebuild} {engine} />
</SvelteFlowProvider>
