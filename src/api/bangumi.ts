import { API_BASE as GLOBAL_API_BASE, authorizedFetch } from './client';
import { withApiSessionToken } from './sessionToken';

const BASE_PATH = '/api/integrations/bangumi';
const API_BASE = GLOBAL_API_BASE ? `${GLOBAL_API_BASE}${BASE_PATH}` : BASE_PATH;

type BangumiImageKind = 'avatar' | 'subject';

/** Resolve and proxy Bangumi images for both Vite and Tauri production. */
export function buildBangumiImageUrl(
  path: string | null | undefined,
  kind: BangumiImageKind = 'avatar',
): string {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) {
    const endpoint = kind === 'subject' ? 'subject-image' : 'avatar';
    return withApiSessionToken(`${API_BASE}/${endpoint}?url=${encodeURIComponent(path)}`);
  }
  return withApiSessionToken(`${GLOBAL_API_BASE}${path}`);
}

export interface BangumiUser {
  id: number;
  username: string;
  nickname: string;
  avatar: string;
  sign: string;
}

export interface BangumiSession {
  credential_state: 'found' | 'not_found' | 'unavailable';
  credential_saved: boolean;
  auth_status: 'unknown' | 'valid' | 'reauth_required';
  connectivity: 'unknown' | 'online' | 'offline' | 'rate_limited' | 'forbidden' | 'server_error';
  status: string; // 兼容派生值：signed_out / unavailable / available / connected
  user: BangumiUser | null;
  last_verified_at: string;
  last_success_at: string;
  last_failure_at: string;
  last_http_status: number | null;
  last_error_code: string;
  last_error_message: string;
}

type FetchJsonOptions = RequestInit & {
  timeoutMs?: number;
};

async function fetchJson<T>(url: string, options: FetchJsonOptions = {}): Promise<T> {
  const { timeoutMs = 10_000, signal, ...fetchOptions } = options;
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener('abort', abort, { once: true });
  }
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await authorizedFetch(url, {
    ...fetchOptions,
    headers: {
      'Content-Type': 'application/json',
      ...fetchOptions.headers,
    },
      signal: controller.signal,
  });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('Bangumi 请求超时，请稍后再试');
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  return response.json();
}

export const bangumiApi = {
  async setToken(accessToken: string): Promise<{ ok: boolean; me: BangumiUser }> {
    return fetchJson(`${API_BASE}/token`, {
      method: 'POST',
      body: JSON.stringify({ access_token: accessToken }),
      timeoutMs: 15_000,
    });
  },

  async clearToken(): Promise<{ ok: boolean }> {
    return fetchJson(`${API_BASE}/token`, {
      method: 'DELETE',
    });
  },

  async getMe(): Promise<BangumiUser> {
    return fetchJson(`${API_BASE}/me`, { timeoutMs: 4_000 });
  },

  async getSession(): Promise<BangumiSession> {
    return fetchJson(`${API_BASE}/session`, { timeoutMs: 6_000 });
  },

  async verifySession(): Promise<BangumiSession> {
    return fetchJson(`${API_BASE}/session/verify`, {
      method: 'POST',
      timeoutMs: 12_000,
    });
  },

  async searchSubjects(keyword: string, limit = 10, offset = 0, subjectTypes: number[] = []): Promise<any> {
    return fetchJson(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify({ keyword, limit, offset, subject_types: subjectTypes }),
      timeoutMs: 12_000,
    });
  },

};
