import { API_BASE as GLOBAL_API_BASE, authorizedFetch } from './client';

const BASE_PATH = '/api/scrape';
const API_BASE = GLOBAL_API_BASE ? `${GLOBAL_API_BASE}${BASE_PATH}` : BASE_PATH;

export interface ScrapeTarget {
  scrape_target_id: string;
  source: string;
  import_plan_id: string;
  work_id: string;
  card_type: string;
  media_type: string;
  series_group: string;
  local_title: string;
  original_title: string;
  source_subwork_dir: string;
  local_year: number | null;
  local_season_number: number | null;
  scrape_title: string;
  scrape_year: number | null;
  scrape_type: string;
  show_type: string;
  group_type: string;
  item_ids: string[];
  local_episode_count: number;
  tmdb_hint_id: number | null;
  tmdb_hint_type: string;
  needs_review: boolean;
  warnings: string[];
  target_dir: string;
}

export interface ScrapeCandidate {
  candidate_id: string;
  scrape_target_id: string;
  provider: string;
  tmdb_id: number;
  tmdb_type: string;
  title: string;
  original_title: string;
  year: number | null;
  overview: string;
  poster_path: string;
  popularity: number;
  vote_average: number;
  score: number;
  reasons: string[];
  source_meta?: Record<string, any>;
  raw?: Record<string, any>;
}

export interface CandidateSearchResult {
  target: ScrapeTarget;
  search_queries: string[];
  candidates: ScrapeCandidate[];
}

export interface ScrapeTask {
  task_id: string;
  task_type: string;
  source: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed';
  progress: number;
  message: string;
  created_at: string;
  started_at: string;
  finished_at: string;
  error: string;
  result: any;
}

export interface ReviewQueueItem {
  scrape_target_id: string;
  source: string;
  import_plan_id: string;
  series_group: string;
  local_title: string;
  scrape_title: string;
  scrape_year: number | null;
  scrape_type: string;
  local_season_number: number | null;
  reason: string;
  candidates: ScrapeCandidate[];
  added_at: string;
  status: string;
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

export const scrapeApi = {
  async getTargets(source = 'pan115', planId?: string): Promise<ScrapeTarget[]> {
    const params = new URLSearchParams({ source });
    if (planId) params.set('plan_id', planId);
    const payload = await fetchJson<{ targets: ScrapeTarget[] }>(`${API_BASE}/targets?${params}`);
    return payload.targets || [];
  },

  async getTargetByWork(workId: string, source?: string, seasonNumber?: number | null, groupType?: string): Promise<ScrapeTarget | null> {
    const params = new URLSearchParams({ work_id: workId });
    if (source) params.set('source', source);
    if (seasonNumber !== undefined && seasonNumber !== null) params.set('season_number', String(seasonNumber));
    if (groupType) params.set('group_type', groupType);
    const payload = await fetchJson<{ target: ScrapeTarget }>(`${API_BASE}/target-by-work?${params}`);
    return payload.target || null;
  },

  async getCandidates(targetId: string, query?: string, year?: number): Promise<ScrapeCandidate[]> {
    const payload = await this.searchCandidates(targetId, query, year);
    return payload.candidates || [];
  },

  async searchCandidates(targetId: string, query?: string, year?: number): Promise<CandidateSearchResult> {
    const params = new URLSearchParams({ target_id: targetId });
    if (query) params.set('query', query);
    if (year) params.set('year', String(year));
    return fetchJson<CandidateSearchResult>(`${API_BASE}/candidates?${params}`);
  },

  async selectCandidate(
    targetId: string,
    tmdbId: number,
    tmdbType: string,
    selectedBy = 'manual',
    searchQuery?: string,
    tmdbSeasonNumber?: number,
    includeEpisode = true,
    libraryWorkId?: string,
    scope: 'work' | 'season' = 'season',
  ): Promise<any> {
    return fetchJson(`${API_BASE}/select`, {
      method: 'POST',
      body: JSON.stringify({
        target_id: targetId,
        tmdb_id: tmdbId,
        tmdb_type: tmdbType,
        tmdb_season_number: tmdbSeasonNumber,
        selected_by: selectedBy,
        search_query: searchQuery || '',
        include_episode: includeEpisode,
        library_work_id: libraryWorkId || undefined,
        scope,
      }),
    });
  },

  async autoScrape(source = 'pan115', planId?: string): Promise<{ task_id: string; status: string }> {
    return fetchJson(`${API_BASE}/auto`, {
      method: 'POST',
      body: JSON.stringify({ source, plan_id: planId }),
    });
  },

  async backfillCertifications(): Promise<{ task_id: string; status: string }> {
    return fetchJson(`${API_BASE}/certifications/backfill`, { method: 'POST' });
  },

  async getTask(taskId: string): Promise<ScrapeTask> {
    return fetchJson(`${API_BASE}/tasks/${taskId}`);
  },

  async listTasks(source?: string, limit = 20): Promise<ScrapeTask[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set('source', source);
    const payload = await fetchJson<{ tasks: ScrapeTask[] }>(`${API_BASE}/tasks?${params}`);
    return payload.tasks || [];
  },

  async cancelTask(taskId: string): Promise<any> {
    return fetchJson(`${API_BASE}/tasks/${taskId}/cancel`, {
      method: 'POST',
    });
  },

  async getReviewQueue(source?: string): Promise<{ items: ReviewQueueItem[] }> {
    const params = new URLSearchParams();
    if (source) params.set('source', source);
    const suffix = params.toString() ? `?${params}` : '';
    return fetchJson(`${API_BASE}/review-queue${suffix}`);
  },

  async getFailures(): Promise<{ items: any[] }> {
    const payload = await fetchJson<{ items?: any[]; failures?: any[] }>(`${API_BASE}/failures`);
    return { items: payload.items || payload.failures || [] };
  },

  async skipReviewItem(targetId: string): Promise<any> {
    return fetchJson(`${API_BASE}/review-queue/skip`, {
      method: 'POST',
      body: JSON.stringify({ target_id: targetId }),
    });
  },
};
