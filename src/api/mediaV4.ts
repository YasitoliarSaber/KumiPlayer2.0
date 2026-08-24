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

export interface V4SourceLibraryCard {
  root_id: string
  provider: string
  ingest_method: string
  source_mode: string
  last_scan_mode: string
  has_confirmed_baseline: boolean
  source_locator: string
  playback_locator: string
  route_id: string
  display_name: string
  enabled: number
  revision_id: string
  revision_status: string
  revision_created_at: string
  confirmed_at: string
  evidence_count: number
  work_count: number
  asset_count: number
  can_resume: boolean
  job_summary: {
    total: number
    queued: number
    running: number
    succeeded: number
    failed: number
    cancelled: number
  }
}

export interface V4OpenlistBaselineStatus {
  root_id: string
  remote_root: string
  source_mode: string
  last_scan_mode: string
  has_confirmed_baseline: boolean
}

export const mediaV4Api = {
  scan: (request: {
    source?: string
    root_path?: string
    tree_file?: string
    provider?: string
    source_root?: string
    scan_mode?: 'auto' | 'full' | 'incremental'
  }) => api.post<{
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    scan_mode?: 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
    source_mode?: string
    last_scan_mode?: string
    scan_stats?: { requested_directories?: number; rolling_verified?: number; changed_directories?: number }
  }>('/api/v4/sources/scan', request),

  preview: (request: {
    revision_id: string
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    allow_empty?: boolean
    source_display_name?: string
    source_locator?: string
    playback_locator?: string
    source_route_id?: string
    source_mode?: string
  }) => api.post<V4Preview>('/api/v4/imports/preview', request),

  confirm: (revisionId: string) =>
    api.post<{ revision_id: string; status: string; jobs: V4Job[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}/confirm`),

  overrideEvidence: (revisionId: string, evidenceId: string, changes: Record<string, unknown>) =>
    api.patch<V4Preview>(`/api/v4/imports/${encodeURIComponent(revisionId)}/evidence/${encodeURIComponent(evidenceId)}`, { changes }),

  status: (revisionId: string) =>
    api.get<{ revision_id: string; status: string; jobs: V4Job[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}`),

  sourceLibraries: () => api.get<{ cards: V4SourceLibraryCard[] }>('/api/v4/sources/libraries'),

  openlistStatus: (remoteRoot: string) =>
    api.get<V4OpenlistBaselineStatus>(`/api/v4/sources/openlist/status?remote_root=${encodeURIComponent(remoteRoot)}`),

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
