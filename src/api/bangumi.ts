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
  credential_saved: boolean;
  status: 'connected' | 'unavailable' | 'invalid' | 'signed_out';
  user: BangumiUser | null;
  message: string;
}

export interface BangumiMatch {
  work_id: string;
  season_number: number | null;
  subject_id: number;
  subject_name: string;
  subject_name_cn: string;
  confirmed_at: string;
  updated_at: string;
  episode_map: Record<string, number>;
}

export interface BangumiEpisode {
  episode_id: string;
  season_number: number;
  episode_number: number;
  title: string;
  bangumi_episode_id: number | null;
  synced: boolean;
  synced_at: string;
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

  async searchSubjects(keyword: string, limit = 10, offset = 0, subjectTypes: number[] = []): Promise<any> {
    return fetchJson(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify({ keyword, limit, offset, subject_types: subjectTypes }),
      timeoutMs: 12_000,
    });
  },

  async getMatch(workId: string, seasonNumber?: number): Promise<BangumiMatch> {
    const params = seasonNumber !== undefined ? `?season_number=${seasonNumber}` : '';
    return fetchJson(`${API_BASE}/matches/${workId}${params}`);
  },

  async confirmMatch(workId: string, subjectId: number, seasonNumber?: number, subjectName?: string, subjectNameCn?: string): Promise<BangumiMatch> {
    return fetchJson(`${API_BASE}/matches/${workId}`, {
      method: 'POST',
      body: JSON.stringify({
        subject_id: subjectId,
        season_number: seasonNumber,
        subject_name: subjectName || '',
        subject_name_cn: subjectNameCn || '',
      }),
    });
  },

  async removeMatch(workId: string, seasonNumber?: number): Promise<{ ok: boolean }> {
    const params = seasonNumber !== undefined ? `?season_number=${seasonNumber}` : '';
    return fetchJson(`${API_BASE}/matches/${workId}${params}`, {
      method: 'DELETE',
    });
  },

  async setCollection(workId: string, type: number, seasonNumber?: number): Promise<any> {
    return fetchJson(`${API_BASE}/collections/${workId}`, {
      method: 'PATCH',
      body: JSON.stringify({ type, season_number: seasonNumber }),
      timeoutMs: 8_000,
    });
  },

  async getCollection(workId: string, seasonNumber?: number): Promise<any> {
    const params = seasonNumber !== undefined ? `?season_number=${seasonNumber}` : '';
    return fetchJson(`${API_BASE}/collections/${workId}${params}`);
  },

  async getEpisodes(workId: string, seasonNumber?: number): Promise<{ work_id: string; season_number: number | null; match: BangumiMatch | null; match_season_number: number | null; episodes: BangumiEpisode[] }> {
    const params = seasonNumber !== undefined ? `?season_number=${seasonNumber}` : '';
    return fetchJson(`${API_BASE}/episodes/${workId}${params}`);
  },

  async markEpisodeWatched(episodeId: string, workId?: string, seasonNumber?: number, bangumiEpisodeId?: number): Promise<any> {
    return fetchJson(`${API_BASE}/episodes/${episodeId}/watched`, {
      method: 'PUT',
      body: JSON.stringify({
        work_id: workId || '',
        season_number: seasonNumber,
        bangumi_episode_id: bangumiEpisodeId,
        type: 2,
      }),
    });
  },

  async syncProgress(workId: string, seasonNumber?: number): Promise<{
    ok: boolean;
    status: string;
    work_id: string;
    season_number: number | null;
    subject_id: number;
    remote_done_before: number;
    local_done_before: number;
    pulled: number;
    pushed: number;
    pending: number;
  }> {
    return fetchJson(`${API_BASE}/progress/${workId}/sync`, {
      method: 'POST',
      body: JSON.stringify({ season_number: seasonNumber }),
      timeoutMs: 30_000,
    });
  },
};
