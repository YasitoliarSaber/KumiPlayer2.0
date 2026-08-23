import { api } from './client'

export interface V4SourceEvidence {
  evidence_id: string
  scan_id: string
  root_id: string
  source_key: string
  relative_path: string
  entry_kind: string
  provider: string
  source_locator: string
  playback_locator: string
  size?: number | null
  mtime?: number | null
  fingerprint?: string
  ingest_method?: string
}

export interface V4ResolvedWork {
  work_key: string
  preferred_title: string
  year: number | null
  media_type: string
  source_evidence_ids: string[]
}

export interface V4ResolvedEpisode {
  episode_key: string
  work_key: string
  local_season_number: number | null
  local_episode_number: number | null
  absolute_episode_number: number | null
  season_kind: string
  episode_kind: string
  special_number: number | null
  edition_key: string
  asset_evidence_ids: string[]
}

export interface V4ReviewIssue {
  code: string
  evidence_id: string
  message: string
}

export interface V4ResolvedWorkAsset {
  work_key: string
  edition_key: string
  asset_evidence_ids: string[]
}

export interface V4Preview {
  revision_id: string
  status: 'draft' | 'confirmed'
  works: V4ResolvedWork[]
  episodes: V4ResolvedEpisode[]
  work_assets: V4ResolvedWorkAsset[]
  issues: V4ReviewIssue[]
}

export interface V4Job {
  job_id: string
  job_type: string
  revision_id: string
  work_id: string
  status: string
  idempotency_key: string
  attempts?: number
  last_error?: string
}

export interface V4LibraryCard {
  work_id: string
  title: string
  year: number | null
  media_type: string
  episode_count: number
  asset_count: number
}

export const mediaV4Api = {
  scan: (request: {
    source?: string
    root_path?: string
    tree_file?: string
    provider?: string
    source_root?: string
  }) => api.post<{ root_id: string; scan_id: string; entries: V4SourceEvidence[] }>('/api/v4/sources/scan', request),

  preview: (request: {
    revision_id: string
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    allow_empty?: boolean
  }) => api.post<V4Preview>('/api/v4/imports/preview', request),

  confirm: (revisionId: string) =>
    api.post<{ revision_id: string; status: string; jobs: V4Job[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}/confirm`),

  overrideEvidence: (revisionId: string, evidenceId: string, changes: Record<string, unknown>) =>
    api.patch<V4Preview>(`/api/v4/imports/${encodeURIComponent(revisionId)}/evidence/${encodeURIComponent(evidenceId)}`, { changes }),

  status: (revisionId: string) =>
    api.get<{ revision_id: string; status: string; jobs: V4Job[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}`),

  enqueueScrape: (revisionId: string) =>
    api.post<{ revision_id: string; jobs: V4Job[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}/scrape`),

  library: () => api.get<{ generation_id: string; digest: string; cards: V4LibraryCard[] }>('/api/v4/library'),

  saveProgress: (request: {
    work_id: string
    episode_id: string
    asset_id: string
    position: number
    duration: number
    completed?: boolean
  }) => api.post<Record<string, unknown>>('/api/v4/playback/progress', request),

  saveTracking: (request: {
    work_id: string
    provider: string
    provider_id?: string
    last_watched_episode?: number | null
    metadata?: Record<string, unknown>
  }) => api.post<Record<string, unknown>>('/api/v4/tracking/state', request),
}
