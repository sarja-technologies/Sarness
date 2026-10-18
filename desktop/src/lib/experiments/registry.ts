// Experiments the desktop knows how to show. Each one pairs with an engine-side experiment of the same id
// (GET /v1/experiments reports availability); the view is loaded lazily the first time it is launched.
//
// To add an experiment: add an engine package (see engine/sarness_engine/experiments/__init__.py), then an entry
// here with its view component. A launched experiment stays mounted, so its unsaved state survives switching to
// Work and back.

import type { Component } from 'svelte';
import { Palette } from '@lucide/svelte';

export interface ClientExperiment {
  id: string;
  name: string;
  tagline: string;
  icon: Component;
  /** Primary action on the selection screen. */
  cta: string;
  /** Short facts shown on the card. */
  facts: string[];
  /** Example artwork for the card: a painted input and the model's real output for it. */
  example?: { input: string; output: string; caption: string };
  load: () => Promise<{ default: Component<any> }>;
}

export const EXPERIMENTS: ClientExperiment[] = [
  {
    id: 'gods-paintbrush',
    name: "God's Paintbrush",
    tagline: 'Turn simple painted shapes into photorealistic landscapes using AI.',
    icon: Palette,
    cta: "Try God's Paintbrush",
    facts: ['Runs on your server GPU', 'Live preview while you paint', 'Saves to your Library'],
    example: {
      input: new URL('./paintbrush/assets/example-mask.png', import.meta.url).href,
      output: new URL('./paintbrush/assets/example-output.jpg', import.meta.url).href,
      caption: 'A real result: the painting on the left, rendered by the server on the right.',
    },
    load: () => import('./paintbrush/Paintbrush.svelte'),
  },
];

export const experimentById = (id: string) => EXPERIMENTS.find((e) => e.id === id);
