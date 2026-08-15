import { create } from 'zustand';
import { bangumiApi, type BangumiSession, type BangumiUser } from '../api/bangumi';

export type BangumiSessionStatus = 'checking' | 'connected' | 'saved_offline' | 'credential_invalid' | 'signed_out';

/** 后台验证 TTL：上次验证距今小于该值则启动时不触发远程验证。 */
export const SESSION_VERIFY_TTL_MS = 6 * 60 * 60 * 1000;

export interface BangumiSessionView {
  user: BangumiUser | null;
  isLoggedIn: boolean; // 派生 UI 属性：凭据已保存且本地恢复出用户
  sessionStatus: BangumiSessionStatus; // 派生兼容状态
  hasStoredCredential: boolean; // 派生：credential_state != not_found
  credentialState: 'found' | 'not_found' | 'unavailable' | 'unknown';
  authStatus: 'unknown' | 'valid' | 'reauth_required';
  connectivity: 'unknown' | 'online' | 'offline' | 'rate_limited' | 'forbidden' | 'server_error';
  lastVerifiedAt: string;
  lastSuccessAt: string;
  lastHttpStatus: number | null;
  lastErrorCode: string;
  lastErrorMessage: string;
}

interface BangumiState extends BangumiSessionView {
  loading: boolean;
  error: string | null;
  firstConnectionRevision: number;

  restoreSession: () => Promise<BangumiSession | null>;
  verifySession: () => Promise<BangumiSession | null>;
  setToken: (token: string) => Promise<void>;
  clearToken: () => Promise<void>;
}

/** 把后端 session payload 应用到本地状态（单事实源）。 */
function applySession(
  set: (updater: Partial<BangumiState> | ((state: BangumiState) => Partial<BangumiState>)) => void,
  session: BangumiSession,
): void {
  const hasStoredCredential = session.credential_saved;
  const user = session.user;
  let sessionStatus: BangumiSessionStatus;
  if (session.credential_state === 'not_found') {
    sessionStatus = 'signed_out';
  } else if (session.credential_state === 'unavailable') {
    // 凭据存储暂时不可读：保守保留，不判退出
    sessionStatus = 'saved_offline';
  } else if (session.auth_status === 'reauth_required') {
    sessionStatus = 'credential_invalid';
  } else if (user) {
    sessionStatus = 'connected';
  } else {
    // 有凭据但还没有验证快照：等待验证
    sessionStatus = 'saved_offline';
  }
  set((state) => ({
    user,
    isLoggedIn: hasStoredCredential && !!user,
    sessionStatus,
    hasStoredCredential,
    credentialState: session.credential_state,
    authStatus: session.auth_status,
    connectivity: session.connectivity,
    lastVerifiedAt: session.last_verified_at,
    lastSuccessAt: session.last_success_at,
    lastHttpStatus: session.last_http_status,
    lastErrorCode: session.last_error_code,
    lastErrorMessage: session.last_error_message,
    error: session.last_error_message || null,
    loading: false,
    // 本次会话首次连通只发布一次刷新信号
    firstConnectionRevision: state.firstConnectionRevision || (hasStoredCredential && user ? 1 : 0),
  }));
}

const EMPTY_VIEW: BangumiSessionView = {
  user: null,
  isLoggedIn: false,
  sessionStatus: 'checking',
  hasStoredCredential: false,
  credentialState: 'unknown',
  authStatus: 'unknown',
  connectivity: 'unknown',
  lastVerifiedAt: '',
  lastSuccessAt: '',
  lastHttpStatus: null,
  lastErrorCode: '',
  lastErrorMessage: '',
};

export const useBangumiStore = create<BangumiState>()((set) => ({
  ...EMPTY_VIEW,
  loading: false,
  error: null,
  firstConnectionRevision: 0,

  restoreSession: async () => {
    // 只读本地（Credential 三态 + 账户快照），0 个远程请求；单次，不重试
    set({ loading: true, sessionStatus: 'checking', error: null });
    try {
      const session = await bangumiApi.getSession();
      applySession(set, session);
      return session;
    } catch (error) {
      // 本地会话接口失败（后端不可达）：保守保留上次本地状态，不伪装退出
      set({ loading: false, error: (error as Error).message });
      return null;
    }
  },

  verifySession: async () => {
    // 显式远程验证：失败由后端分类进快照并返回 200；网络级失败保留本地状态
    set({ loading: true, error: null });
    try {
      const session = await bangumiApi.verifySession();
      applySession(set, session);
      return session;
    } catch (error) {
      set({ loading: false, error: (error as Error).message });
      return null;
    }
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
        credentialState: 'found',
        authStatus: 'valid',
        connectivity: 'online',
        lastVerifiedAt: new Date().toISOString(),
        lastSuccessAt: new Date().toISOString(),
        lastHttpStatus: 200,
        lastErrorCode: '',
        lastErrorMessage: '',
        error: null,
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
      set({
        ...EMPTY_VIEW,
        sessionStatus: 'signed_out',
        credentialState: 'not_found',
        loading: false,
        error: null,
      });
    } catch (error) {
      set({ loading: false, error: (error as Error).message });
      throw error;
    }
  },
}));
