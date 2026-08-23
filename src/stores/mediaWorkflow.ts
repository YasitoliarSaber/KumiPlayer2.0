import { create } from 'zustand';

interface MediaWorkflowState {
  pendingDroppedTreePath: string | null;
  queueDroppedTreePath: (path: string) => void;
  consumeDroppedTreePath: () => string | null;
}

export const useMediaWorkflowStore = create<MediaWorkflowState>()((set, get) => ({
  pendingDroppedTreePath: null,
  queueDroppedTreePath: (path) => set({ pendingDroppedTreePath: path }),
  consumeDroppedTreePath: () => {
    const path = get().pendingDroppedTreePath;
    if (path) set({ pendingDroppedTreePath: null });
    return path;
  },
}));
