import { API_BASE as GLOBAL_API_BASE, authorizedFetch } from './client';

const BASE_PATH = '/api/system';
const API_BASE = GLOBAL_API_BASE ? `${GLOBAL_API_BASE}${BASE_PATH}` : BASE_PATH;

export interface OpenFolderResult {
  ok: boolean;
  opened: boolean;
  exists: boolean;
  folder_path: string;
  source_path: string;
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await authorizedFetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  return response.json();
}

export const systemApi = {
  async openFolder(workId: string, episodeId?: string, open = true): Promise<OpenFolderResult> {
    return fetchJson(`${API_BASE}/open-folder`, {
      method: 'POST',
      body: JSON.stringify({
        work_id: workId,
        episode_id: episodeId || '',
        open,
      }),
    });
  },

  async openMirrorFolder(workId: string, episodeId?: string, open = true): Promise<OpenFolderResult> {
    return fetchJson(`${API_BASE}/open-folder`, {
      method: 'POST',
      body: JSON.stringify({
        work_id: workId,
        episode_id: episodeId || '',
        folder_type: 'mirror',
        open,
      }),
    });
  },
};
