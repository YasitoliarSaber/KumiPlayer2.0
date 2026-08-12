import { create } from 'zustand';
import { bangumiApi, type BangumiUser } from '../api/bangumi';

export type BangumiSessionStatus = 'checking' | 'connected' | 'saved_offline' | 'credential_invalid' | 'signed_out';

interface BangumiState {
  user: BangumiUser | null;
  isLoggedIn: boolean;
  loading: boolean;
  error: string | null;
  sessionStatus: BangumiSessionStatus;
  hasStoredCredential: boolean;
  firstConnectionRevision: number;

  loadUser: (silent?: boolean) => Promise<void>;
  restoreSession: (maxAttempts?: number) => Promise<void>;
  setToken: (token: string) => Promise<void>;
  clearToken: () => Promise<void>;
}

export const useBangumiStore = create<BangumiState>()((set) => ({
  user: null,
  isLoggedIn: false,
  loading: false,
  error: null,
  sessionStatus: 'checking',
  hasStoredCredential: false,
  firstConnectionRevision: 0,

  loadUser: async (silent = false) => {
    set({ loading: true, error: null });
    try {
      const user = await bangumiApi.getMe();
      set((state) => ({
        user,
        isLoggedIn: true,
        loading: false,
        sessionStatus: 'connected',
        hasStoredCredential: true,
        firstConnectionRevision: state.firstConnectionRevision || 1,
      }));
    } catch (error) {
      set({ user: null, isLoggedIn: false, loading: false, sessionStatus: 'signed_out', error: silent ? null : (error as Error).message });
    }
  },

  restoreSession: async (maxAttempts = 4) => {
    set({ loading: true, sessionStatus: 'checking', error: null });
    let lastMessage = '';
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      if (attempt > 0) {
        const delay = Math.min(6_000, 1_500 * (2 ** (attempt - 1)));
        await new Promise((resolve) => window.setTimeout(resolve, delay));
      }
      try {
        const session = await bangumiApi.getSession();
        if (session.status === 'connected' && session.user) {
          set((state) => ({
            user: session.user,
            isLoggedIn: true,
            loading: false,
            error: null,
            sessionStatus: 'connected',
            hasStoredCredential: true,
            firstConnectionRevision: state.firstConnectionRevision || 1,
          }));
          return;
        }
        if (session.status === 'signed_out') {
          set({ user: null, isLoggedIn: false, loading: false, error: null, sessionStatus: 'signed_out', hasStoredCredential: false });
          return;
        }
        if (session.status === 'invalid') {
          set({
            user: null,
            isLoggedIn: false,
            loading: false,
            error: session.message || 'Bangumi 登录信息已失效，请更新 Access Token',
            sessionStatus: 'credential_invalid',
            hasStoredCredential: true,
          });
          return;
        }
        lastMessage = session.message;
      } catch (error) {
        lastMessage = (error as Error).message;
      }
    }
    set({
      user: null,
      isLoggedIn: false,
      loading: false,
      error: lastMessage || '暂时无法连接 Bangumi',
      sessionStatus: 'saved_offline',
      hasStoredCredential: true,
    });
  },

  setToken: async (token: string) => {
    set({ loading: true, error: null });
    try {
      const result = await bangumiApi.setToken(token);
      set((state) => ({
        user: result.me,
        isLoggedIn: true,
        loading: false,
        sessionStatus: 'connected',
        hasStoredCredential: true,
        firstConnectionRevision: state.firstConnectionRevision || 1,
      }));
    } catch (error) {
      set({ loading: false, error: (error as Error).message });
      throw error;
    }
  },

  clearToken: async () => {
    set({ loading: true, error: null });
    try {
      await bangumiApi.clearToken();
      set({ user: null, isLoggedIn: false, loading: false, sessionStatus: 'signed_out', hasStoredCredential: false });
    } catch (error) {
      set({ loading: false, error: (error as Error).message });
      throw error;
    }
  },
}));
