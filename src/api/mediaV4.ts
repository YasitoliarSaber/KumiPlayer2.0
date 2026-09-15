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
  /** 阻断确认的 issue 数（提示类不计入）；旧后端响应缺失时回退到 issues.length。 */
  blocking_issue_count?: number
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

export type V4MetadataRecoveryAction =
  | 'choose_candidate'
  | 'retry_metadata'
  | 'check_settings'
  | 'review_identity'
  | 'none'

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
  overall_status: 'running' | 'needs_attention' | 'queued' | 'cancelled' | 'completed'
  /** 来源卡当前位于扫描、识别复核还是已确认导入阶段。旧后端可能不返回。 */
  phase?: 'scan' | 'review' | 'execute' | string
  scan?: {
    scan_id: string
    status: string
    stage: string
    stage_label: string
    processed_count: number
    total_count: number
    progress: number | null
    heartbeat_at: string
    cancel_requested?: boolean
  } | null
  attention_count: number
  /** 尚未建立的作品关联，不阻断导入或播放，与当前故障数量分开。 */
  relation_pending_count?: number
  last_error: string
  source_locator: string
  /** 面向来源卡的紧凑路径摘要；原始 locator 仍用于恢复来源配置。 */
  display_path?: string
  playback_locator: string
  route_id: string
  display_name: string
  enabled: number
  added_at: string
  updated_at: string
  revision_id: string
  latest_revision_id: string
  revision_state: string
  evidence_count: number
  work_count: number
  asset_count: number
  progress: {
    state: string
    stage: string
    current_work_id: string
    current_work_title: string
    completed_work_count: number
    total_work_count: number
    percent: number | null
    message: string
  }
  active_task?: {
    kind: 'scan' | 'execution'
    revision_id: string
    status: 'queued' | 'running' | 'cancelling' | 'interrupted' | string
    stage: string
    label: string
    percent: number | null
    can_cancel: boolean
    cancel_requested: boolean
  } | null
  available_actions: string[]
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

export interface V4MaintenancePreview {
  preview_id: string
  scope: string
  created_at: string
  expires_at: string
  root_ids: string[]
  root_count: number
  /** 尚未确认导入，因此没有可清理媒体库数据的来源根数量。 */
  skipped_root_count?: number
  /** 仅按内容来源聚合的跳过说明，不暴露内部 root ID。 */
  skipped_provider_counts?: Array<{ provider: string; count: number }>
  work_count: number
  orphan_work_count: number
  mixed_work_count: number
  asset_count: number
  artifact_count: number
  /** 相对镜像根的脱敏摘要（至多 20 条），绝不含完整本地绝对路径。 */
  artifact_summaries: string[]
  blocked: boolean
  blocked_job_count: number
  blocked_job_types: string[]
  history_count?: number
  progress_count?: number
  tracking_count?: number
  warnings: string[]
  /** 可读来源根标识（root_id + provider），不含 source locator。 */
  root_names: Array<{ root_id: string; provider: string }>
  digest: string
}

export interface V4MaintenanceResult {
  preview_id: string
  scope: string
  status: string
  retired_root_count: number
  root_names: Array<{ root_id: string; provider: string }>
  orphan_work_count: number
  mixed_work_count: number
  artifact_count: number
  /** 相对镜像根的脱敏摘要（至多 20 条）。 */
  artifact_results: Array<{ path: string; status: string; error?: string }>
  projection_status: string
}

export interface V4IdentityRepairPreview {
  preview_id: string
  work_id: string
  old_work: { work_id: string; identity_key: string; title: string }
  target_works: Array<{
    work_key: string
    title: string
    media_type: string
    work_type: string
    card_type: string
    relation_type: string
  }>
  items: Array<{
    asset_id: string
    evidence_id: string
    relative_path: string
    target_work_key: string
    local_season_number: number | null
    local_episode_number: number | null
  }>
  provider_assignments: Array<{
    provider: string
    media_type: string
    provider_id: string
    target_work_key: string
  }>
  blocked: boolean
  blocked_reasons: Array<{ reason_code: string; reason: string; asset_id?: string; provider?: string; provider_id?: string }>
  digest: string
  created_at: string
  expires_at: string
}

export interface V4IdentityRepairResult {
  preview_id: string
  status: string
  old_work_id: string
  target_work_ids: Record<string, string>
  new_revision_ids: string[]
  migrated_asset_count: number
  migrated_progress_count: number
}

export interface V4DraftSummary {
  revision_id: string
  root_id: string
  scan_id: string
  created_at: string
  provider: string
  source_mode: string
  source_locator: string
  playback_locator: string
  evidence_count: number
  issue_count: number
}

export interface V4OpenlistBaselineStatus {
  root_id: string
  remote_root: string
  source_mode: string
  last_scan_mode: string
  has_confirmed_baseline: boolean
}

export interface V4WorkProgressUnit {
  work_id: string
  title: string
  media_type: 'tv' | 'movie'
  episode_count: number
  asset_count: number
  overall_status:
    | 'waiting_mirror'
    | 'running_mirror'
    | 'waiting_metadata'
    | 'running_metadata'
    | 'needs_attention'
    | 'failed'
    | 'cancelled'
    | 'completed'
  metadata_state: string
  metadata_reason: string
  metadata_reason_code?: string
  artifact_state?: string
  artifact_reasons?: string[]
  metadata_warning?: string
  metadata_recovery_action?: V4MetadataRecoveryAction
  metadata_recovery_hint?: string
  /** 每次本作品任务或刮削快照更新时变化，用于使执行详情缓存失效。 */
  detail_version?: string
  mirror: { job_id: string; status: string; attempts: number; last_error: string }
  metadata: { job_id: string; status: string; attempts: number; last_error: string }
}

export interface V4WorkExecutionDetail {
  revision_id: string
  work_id: string
  work: {
    title: string
    media_type: string
    provider: string
    provider_id: string
    metadata_state: string
    metadata_reason: string
    metadata_reason_code?: string
    artifact_state?: string
    artifact_reasons?: string[]
    metadata_warning?: string
    metadata_recovery_action?: V4MetadataRecoveryAction
    metadata_recovery_hint?: string
  }
  mirror: {
    status: string
    error: string
    artifact_count: number
    artifacts: Array<{ file_name: string; status: string }>
  }
  metadata_job_status: string
  scrape?: {
    metadata_state: string
    metadata_reason?: string
    metadata_reason_code?: string
    artifact_state?: string
    artifact_reasons?: string[]
    metadata_warning?: string
    metadata_recovery_action?: V4MetadataRecoveryAction
    metadata_recovery_hint?: string
    title: string
    original_title: string
    year: number | null
    plot: string
    rating: number | null
    runtime: number | null
    genres: string[]
    studios: string[]
    premiered: string
    identity_status?: string
    work_metadata_status?: string
    episode_mapping_status?: string
    failure_stage?: string
    retryable?: boolean
    season_results?: Array<{
      local_season_number: number | null
      provider_season_number: number | null
      status: string
      reason_code: string
      failure_stage: string
      retryable: boolean
    }>
    candidate_decision: {
      decision: string
      reason: string
      selected_provider: string
      selected_provider_id: string
      selected_score: number | null
      ranked_candidates: Array<{
        provider: string
        provider_id: string
        media_type: string
        title: string
        original_title: string
        year: number | null
        score: number | null
        reasons: string[]
        recommended: boolean
        identity_safe: boolean
        blocked: boolean
      }>
    } | null
  }
  seasons: Array<{ season_number: number; season_kind: string; title: string; episode_count: number }>
  episodes: Array<{
    episode_id: string
    season_number: number
    season_kind: string
    episode_number: number | null
    display_title: string
    scraped_title: string
    scraped_plot: string
    provider_episode_number: number | null
    provider_episode_id: string
    runtime: number | null
    still_url?: string
    mapped: boolean
    file_name: string
    playback_ready: boolean
    playback_locator_available?: boolean
  }>
  episode_total: number
  episodes_truncated: boolean
  next_episode_offset: number | null
  has_detail: boolean
}

export interface V4StageSummary {
  status: 'idle' | 'running' | 'failed' | 'cancelled' | 'queued' | 'succeeded' | 'needs_attention'
  total: number
  queued: number
  running: number
  succeeded: number
  failed: number
  cancelled: number
  needs_attention?: number
}

export interface V4ExecutionProgress {
  revision_id: string
  revision_status: string
  overall_status: 'running' | 'needs_attention' | 'queued' | 'cancelled' | 'completed'
  stage_summary: {
    mirror: V4StageSummary
    metadata: V4StageSummary
    projection: V4StageSummary
  }
  work_units: V4WorkProgressUnit[]
}

export interface V4TrackingWork {
  work_id: string
  title: string
  show_type: string
  media_type: 'tv' | 'movie'
  status: 'watching' | 'on_hold'
  favorite: boolean
  last_watched_episode: number | null
  latest_episode_number: number | null
  updated_at: string
}

export interface V4TrackingScanTask {
  task_id: string
  root_id: string
  remote_root: string
  status: string
  reason?: string
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

  cancelImport: (revisionId: string) =>
    api.post<{ revision_id: string; running: number; cancelled: number }>(`/api/v4/imports/${encodeURIComponent(revisionId)}/cancel`),

  overrideEvidence: (revisionId: string, evidenceId: string, changes: Record<string, unknown>) =>
    api.patch<V4Preview>(`/api/v4/imports/${encodeURIComponent(revisionId)}/evidence/${encodeURIComponent(evidenceId)}`, { changes }),

  status: (revisionId: string) =>
    api.get<{ revision_id: string; status: string; jobs: V4Job[]; progress: V4ExecutionProgress }>(`/api/v4/imports/${encodeURIComponent(revisionId)}`),
  workExecutionDetail: (revisionId: string, workId: string, episodeOffset = 0) =>
    api.get<V4WorkExecutionDetail>(
      `/api/v4/revisions/${encodeURIComponent(revisionId)}/works/${encodeURIComponent(workId)}/execution-detail?episode_offset=${episodeOffset}`,
    ),

  sourceLibraries: () => api.get<{ cards: V4SourceLibraryCard[] }>('/api/v4/sources/libraries'),

  hideSourceLibraryCard: (rootId: string) =>
    api.delete<{ root_id: string; hidden: boolean }>(`/api/v4/sources/libraries/${encodeURIComponent(rootId)}`),

  renameSourceLibraryCard: (rootId: string, displayName: string) =>
    api.patch<{ root_id: string; display_name: string }>(
      `/api/v4/sources/libraries/${encodeURIComponent(rootId)}`,
      { display_name: displayName },
    ),

  drafts: () => api.get<{ drafts: V4DraftSummary[] }>('/api/v4/sources/drafts'),

  maintenancePreview: (scope: string) =>
    api.post<V4MaintenancePreview>('/api/v4/library-maintenance/delete-preview', { scope }),

  maintenanceConfirm: (request: { preview_id: string; scope: string; digest: string }) =>
    api.post<V4MaintenanceResult>('/api/v4/library-maintenance/delete-confirm', request),

  maintenanceResume: (previewId: string) =>
    api.post<V4MaintenanceResult>('/api/v4/library-maintenance/delete-resume', { preview_id: previewId }),

  identityRepairPreview: (workId: string) =>
    api.post<V4IdentityRepairPreview>(`/api/v4/works/${encodeURIComponent(workId)}/identity-repair-preview`, {}),

  identityRepairApply: (request: { preview_id: string; digest: string }) =>
    api.post<V4IdentityRepairResult>('/api/v4/identity-repair/apply', request),

  identityRepairResume: (operationId: string) =>
    api.post<V4IdentityRepairResult>('/api/v4/identity-repair/resume', { operation_id: operationId }),

  setWorkTitle: (workId: string, title: string) =>
    api.patch<{ work_id: string; title: string }>(`/api/v4/works/${encodeURIComponent(workId)}/title`, { title }),

  restoreWorkTitle: (workId: string) =>
    api.delete<{ work_id: string; restored: boolean }>(`/api/v4/works/${encodeURIComponent(workId)}/title`),

  uploadWorkArtwork: (workId: string, kind: string, dataBase64: string) =>
    api.post<{ path: string }>(`/api/v4/works/${encodeURIComponent(workId)}/artwork`, { kind, data_base64: dataBase64 }),

  restoreWorkArtwork: (workId: string, kind: string) =>
    api.delete<{ work_id: string; restored: boolean }>(`/api/v4/works/${encodeURIComponent(workId)}/artwork/${kind}`),

  workFolder: (workId: string) =>
    api.get<{ folder: string; exists: boolean }>(`/api/v4/works/${encodeURIComponent(workId)}/folder`),

  enqueueWorkScrape: (workId: string) =>
    api.post<{ work_id: string; job_id: string; status: string }>(`/api/v4/works/${encodeURIComponent(workId)}/scrape`),

  trackingWorks: () => api.get<{ works: V4TrackingWork[] }>('/api/v4/tracking/works'),

  trackingScanAll: () => api.post<{ tasks: V4TrackingScanTask[] }>('/api/v4/tracking/scan-all', {}),

  trackingScanWork: (workId: string) =>
    api.post<{ task_id: string; root_id: string; remote_root: string; status: string }>(`/api/v4/tracking/${encodeURIComponent(workId)}/scan`, {}),

  trackingCancelScan: (scanId: string) =>
    api.post<{ scan_id: string; status: string }>(`/api/v4/tracking/scans/${encodeURIComponent(scanId)}/cancel`),

  metadataSearch: (request: { work_id: string; query?: string; media_type?: string; year?: number | null }) =>
    api.post<{ work_id: string; candidates: Array<{ candidate_id: string; provider_id: string; media_type: string; title: string; original_title: string; year: number | null; aliases: string[] }> }>('/api/v4/metadata/search', request),

  metadataRetry: (workId: string) =>
    api.post<{ work_id: string; revision_id: string; job_id: string; status: string; metadata_recovery_action: V4MetadataRecoveryAction }>('/api/v4/metadata/retry', { work_id: workId }),
  /** 只重新下载缺失的图片产物，不重新搜索在线资料。 */
  metadataArtifactsRetry: (workId: string) =>
    api.post<{ work_id: string; revision_id: string; binding_status: string; metadata_state: string; artifact_state: string; artifact_reasons: string[] }>('/api/v4/metadata/artifacts/retry', { work_id: workId }),

  metadataConfirm: (request: { work_id: string; candidate_id: string }) =>
    api.post<{ work_id: string; candidate_id: string; provider: string; provider_id: string; status: string }>('/api/v4/metadata/confirm', request),

  deleteWorkPreview: (workId: string) =>
    api.post<{ preview_id: string; work_id: string; artifact_count: number; artifact_paths: string[]; playback_count: number; tracking_count: number; digest: string }>(`/api/v4/works/${encodeURIComponent(workId)}/delete-preview`, {}),

  deleteWorkConfirm: (workId: string, previewId: string, digest: string) =>
    api.post<{ preview_id: string; work_id: string; status: string; artifact_results: Array<{ path: string; status: string }> }>(`/api/v4/works/${encodeURIComponent(workId)}/delete-confirm`, { preview_id: previewId, digest }),

  revisionEvidence: (revisionId: string) =>
    api.get<{ revision_id: string; status: string; entries: V4SourceEvidence[] }>(`/api/v4/imports/${encodeURIComponent(revisionId)}/evidence`),

  openlistStatus: (remoteRoot: string) =>
    api.get<V4OpenlistBaselineStatus>(`/api/v4/sources/openlist/status?remote_root=${encodeURIComponent(remoteRoot)}`),

  startDurableScan: (request: {
    source?: string
    root_path?: string
    tree_file?: string
    provider?: string
    source_root?: string
    scan_mode?: 'full' | 'incremental'
    revision_id?: string
    source_display_name?: string
  }) => api.post<{ scan_id: string; root_id: string; scan_mode: string; source_mode?: string; status: string }>('/api/v4/sources/scans', request),

  durableScan: (scanId: string, includeEntries = false) =>
    api.get<{ scan_id: string; root_id: string; status: string; started_at: string; finished_at: string; error: string; evidence_count: number; parsed_count?: number; stage: string; stage_label: string; processed_count: number; total_count: number; discovered_count?: number; progress?: number | null; heartbeat_at?: string; cancel_requested?: boolean; entries: V4SourceEvidence[] }>(`/api/v4/sources/scans/${encodeURIComponent(scanId)}?include_entries=${includeEntries ? 'true' : 'false'}`),

  cancelDurableScan: (scanId: string) =>
    api.post<{ scan_id: string; status: string }>(`/api/v4/sources/scans/${encodeURIComponent(scanId)}/cancel`),

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
