// Persisted UI preferences (per desktop). Engine-side data never lives here.

export type View = 'workspace' | 'system' | 'library' | 'models' | 'settings' | 'experiments';
export type WorkView = Exclude<View, 'experiments'>;
export const VIEWS: View[] = ['workspace', 'system', 'library', 'models', 'settings', 'experiments'];

interface Prefs {
  view: View;
  /** Last view of the Work space, restored when switching back from Experiments. */
  workView: WorkView;
  sidebarCollapsed: boolean;
  panelSizes: Record<string, number>;
  imageMode: 'simple' | 'advanced';
  videoMode: 'simple' | 'advanced';
  libraryLayout: 'grid' | 'dense';
  libraryThumb: number;
  projectId: string | null;
  folderId: string | null;
  chatShowReasoning: boolean;
  reduceMotion: boolean;
  theme: 'light' | 'dark' | 'system';
  lastChatModel: string | null;
}

const KEY = 'sarness.prefs.v1';

const defaults: Prefs = {
  view: 'workspace',
  workView: 'workspace',
  sidebarCollapsed: false,
  panelSizes: {},
  imageMode: 'simple',
  videoMode: 'simple',
  libraryLayout: 'grid',
  libraryThumb: 220,
  projectId: null,
  folderId: null,
  chatShowReasoning: false,
  reduceMotion: false,
  theme: 'light',
  lastChatModel: null,
};

function load(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const v = { ...defaults, ...JSON.parse(raw) } as Prefs;
      if (!VIEWS.includes(v.view)) v.view = 'workspace';
      if (!VIEWS.includes(v.workView) || v.workView === ('experiments' as View)) v.workView = 'workspace';
      if (!['light', 'dark', 'system'].includes(v.theme)) v.theme = 'light';
      return v;
    }
  } catch {
    /* ignore */
  }
  return { ...defaults };
}

export const prefs = $state<Prefs>(load());

let timer: ReturnType<typeof setTimeout> | null = null;
export function persistPrefs() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify($state.snapshot(prefs)));
    } catch {
      /* storage full or unavailable */
    }
  }, 150);
}

/** Generic persisted state for feature-specific drafts (e.g. generation settings). */
export function persisted<T extends object>(key: string, initial: T): T {
  let value: T = initial;
  try {
    const raw = localStorage.getItem(`sarness.${key}`);
    if (raw) value = { ...initial, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  const s = $state<T>(value);
  return s;
}

export function savePersisted<T>(key: string, value: T) {
  try {
    localStorage.setItem(`sarness.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}
