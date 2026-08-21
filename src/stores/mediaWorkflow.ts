import { create } from 'zustand';
import type { ImportPreview, SourcePathValidation, TaskRecord } from '../api/types';

export type MediaWorkflowSource = 'pan115' | 'baidu' | 'local' | 'openlist';
export type MediaWorkflowStep = 'import' | 'confirm' | 'workbench' | 'background' | 'maintenance';
export type MediaWorkflowEntryStatus = 'idle' | 'parsing' | 'parsed' | 'failed';
export type MediaWorkflowFamily = 'anime' | 'live';
export type MediaWorkflowImportScope = '' | 'seasonal';

export interface MediaWorkflowEntry {
  id: string;
  path: string;
  note: string;
  status: MediaWorkflowEntryStatus;
  presetId?: string;
  planId?: string;
  /** RWK-35：TXT baseline 的 root 级确认身份（多作品一次确认全部 revisions） */
  confirmationRootId?: string;
  confirmationGeneration?: number;
  /** RWK-38（P0-3）：baseline 失败/未完成时禁止确认与自动 pipeline（绝不回退 legacy） */
  confirmationBlocked?: boolean;
  preview?: ImportPreview;
  resolvedRoot?: string;
  pathValidation?: SourcePathValidation;
  error?: string;
}

export interface BackgroundImportSession {
  source: MediaWorkflowSource;
  batchId: string;
}

interface MediaWorkflowState {
  step: MediaWorkflowStep;
  source: MediaWorkflowSource;
  family: MediaWorkflowFamily;
  importScope: MediaWorkflowImportScope;
  entries: MediaWorkflowEntry[];
  activeEntryId: string;
  task: TaskRecord | null;
  taskKind: 'mirror' | 'scrape' | null;
  backgroundImport: BackgroundImportSession | null;
  pendingDroppedTreePath: string | null;
  setStep: (step: MediaWorkflowStep) => void;
  setSource: (source: MediaWorkflowSource) => void;
  setFamily: (family: MediaWorkflowFamily) => void;
  setImportScope: (scope: MediaWorkflowImportScope) => void;
  setEntries: (entries: MediaWorkflowEntry[] | ((entries: MediaWorkflowEntry[]) => MediaWorkflowEntry[])) => void;
  setActiveEntryId: (id: string) => void;
  setTask: (task: TaskRecord | null) => void;
  setTaskKind: (kind: 'mirror' | 'scrape' | null) => void;
  setBackgroundImport: (backgroundImport: BackgroundImportSession | null) => void;
  queueDroppedTreePath: (path: string) => void;
  consumeDroppedTreePath: () => string | null;
}

function createEntry(): MediaWorkflowEntry {
  return { id: crypto.randomUUID(), path: '', note: '', status: 'idle' };
}

const initialEntry = createEntry();

export const useMediaWorkflowStore = create<MediaWorkflowState>((set, get) => ({
  step: 'import',
  source: 'local',
  family: 'anime',
  importScope: '',
  entries: [initialEntry],
  activeEntryId: initialEntry.id,
  task: null,
  taskKind: null,
  backgroundImport: null,
  pendingDroppedTreePath: null,
  setStep: (step) => set({ step }),
  setSource: (source) => set((state) => state.source === source
    ? { source }
    : { source, importScope: '' }),
  setFamily: (family) => set((state) => state.family === family
    ? { family }
    : { family, importScope: '' }),
  setImportScope: (importScope) => set({ importScope }),
  setEntries: (entries) => set((state) => ({
    entries: typeof entries === 'function' ? entries(state.entries) : entries,
  })),
  setActiveEntryId: (activeEntryId) => set({ activeEntryId }),
  setTask: (task) => set({ task }),
  setTaskKind: (taskKind) => set({ taskKind }),
  setBackgroundImport: (backgroundImport) => set({ backgroundImport }),
  queueDroppedTreePath: (pendingDroppedTreePath) => set({ pendingDroppedTreePath }),
  consumeDroppedTreePath: () => {
    const path = get().pendingDroppedTreePath;
    if (path) set({ pendingDroppedTreePath: null });
    return path;
  },
}));
