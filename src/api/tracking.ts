import { api } from './client'
import type { TrackingBinding } from './types'

export interface ManualEpisodePreviewItem {
  item_id: string
  path: string
  season_number: number | null
  episode_number: number | null
  title: string
  status: 'added' | 'existing' | 'replaced' | 'conflict' | 'unrecognized'
}

export const trackingApi = {
  list: () => api.get<{ items: TrackingBinding[] }>('/api/tracking/works'),
  create: (input: {
    work_id?: string
    display_title: string
    logical_source?: 'local' | 'pan115' | 'baidu'
    root_path: string
    season_number?: number | null
  }) => api.post<TrackingBinding>('/api/tracking/works', input),
  update: (workId: string, patch: Partial<Pick<TrackingBinding, 'tracking_state' | 'attention_state' | 'root_path' | 'season_number'>>) =>
    api.patch<TrackingBinding>(`/api/tracking/works/${encodeURIComponent(workId)}`, patch),
  scan: (workId: string, includeScrape = true) =>
    api.post<{ task_id: string; status: string }>(`/api/tracking/works/${encodeURIComponent(workId)}/scan`, { include_scrape: includeScrape }),
  scanAll: (input: {
    includeScrape?: boolean
    source: 'all' | 'local' | 'pan115' | 'baidu'
    workIds: string[]
  }) =>
    api.post<{ task_id: string; status: string }>('/api/tracking/scan-all', {
      include_scrape: input.includeScrape ?? true,
      source: input.source,
      work_ids: input.workIds,
    }),
  importRoot: (input: {
    rootPath: string
    source: 'local' | 'pan115' | 'baidu'
    includeScrape?: boolean
  }) =>
    api.post<{ task_id: string; status: string }>('/api/tracking/import-root', {
      root_path: input.rootPath,
      logical_source: input.source,
      include_scrape: input.includeScrape ?? true,
    }),
  uploadArtwork: (workId: string, kind: 'poster' | 'fanart' | 'clearlogo', file: File) => {
    const body = new FormData()
    body.append('file', file)
    return api.form<{ path: string; provenance: 'manual' }>(
      `/api/library/works/${encodeURIComponent(workId)}/artwork/${kind}`, 'PUT', body,
    )
  },
  restoreArtwork: (workId: string, kind: 'poster' | 'fanart' | 'clearlogo') =>
    api.delete<{ restored: boolean }>(`/api/library/works/${encodeURIComponent(workId)}/artwork/${kind}`),
  previewEpisodes: (workId: string, paths: string[], seasonNumber: number | null) =>
    api.post<{ plan_id: string; can_commit: boolean; items: ManualEpisodePreviewItem[] }>(
      `/api/library/works/${encodeURIComponent(workId)}/episodes/preview`,
      { paths, season_number: seasonNumber },
    ),
  commitEpisodes: (workId: string, planId: string) =>
    api.post<{ task_id: string; status: string }>(
      `/api/library/works/${encodeURIComponent(workId)}/episodes/commit`,
      { plan_id: planId, auto_scrape: true },
    ),
}
