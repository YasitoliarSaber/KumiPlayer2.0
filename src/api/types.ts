// KumiPlayer 2.0 API 类型定义

// 来源（连接/导入方式兼容字段；真实内容提供商见 ProviderId）
export type SourceId = 'all' | 'pan115' | 'baidu' | 'local' | 'openlist'
export type ImportSourceId = 'pan115' | 'baidu' | 'local' | 'openlist'

// 内容提供商（OpenList 路由/媒体预设的真实归属）
export type ProviderId = 'pan115' | 'baidu' | 'quark' | 'other' | 'local'

// 导入方式
export type IngestMethod = 'openlist_api' | 'directory_tree' | 'local_scan'

// OpenList 提供商路由
export interface OpenListRoute {
  route_id: string
  label: string
  remote_prefix: string
  provider_id: ProviderId
  enabled: boolean
  local_path?: string
  local_available?: boolean
}

// 页面
export type PageId = 'library' | 'import' | 'scrape' | 'tasks' | 'settings' | 'work-detail'

// 分类
export type CategoryId = 'all' | 'anime' | 'anime-movie' | 'series' | 'movie'

// 排序
export type SortId =
  | 'default'
  | 'recent'
  | 'ratingAsc'
  | 'ratingDesc'
  | 'episodesAsc'
  | 'episodesDesc'
  | 'yearAsc'
  | 'yearDesc'

// 主题
export type ThemeId = 'light' | 'dark'

// 媒体库
export interface LibraryIndex {
  works: WorkIndex[]
  source_summary?: Record<string, SourceSummary>
  summary?: Record<string, unknown>
  generated_at: string
  needs_rescan?: boolean
}

export interface SourceSummary {
  work_count: number
  episode_count: number
  strm_count: number
  missing_strm_count: number
  orphan_strm_count: number
  scraped_work_count: number
  poster_count: number
  fanart_count: number
  clearlogo_count: number
  warnings: string[]
}

export interface WorkIndex {
  work_id: string
  title: string
  original_title: string
  title_provenance?: 'manual' | 'nfo' | 'online'
  year: number | null
  rating: number
  plot: string
  genres: string[]
  studios: string[]
  show_type: 'anime_series' | 'anime_movie' | 'live_series' | 'live_movie' | ''
  media_type: 'tv' | 'movie' | ''
  source: 'pan115' | 'baidu' | 'local' | 'openlist'
  sources?: Array<'pan115' | 'baidu' | 'local' | 'openlist'>
  provider_id?: ProviderId
  ingest_method?: IngestMethod
  source_route_id?: string
  source_episode_ids?: Partial<Record<ImportSourceId, string>>
  import_scope?: 'seasonal' | ''
  card_type: 'main_series' | 'standalone'
  poster_path: string
  fanart_path: string
  local_poster_path?: string
  local_fanart_path?: string
  clearlogo_path: string
  dir_path: string
  seasons: SeasonIndex[]
  episodes: EpisodeIndex[]
  related_works: RelatedWork[]
  cast?: Array<{ name: string; role: string; profile_path: string }>
  tags: string[]
  last_played: string | null
  tracking?: TrackingBinding | null
  metadata_state?: 'ready' | 'waiting_metadata' | 'waiting_review' | 'source_unavailable'
  episode_count?: number
  main_episode_count?: number
  latest_episode_number?: number
  certification?: string
  certification_country?: string
  artwork_provenance?: Record<'poster' | 'fanart' | 'clearlogo', string>
  watch_status?: {
    work_id: string
    status: '' | 'watching' | 'watched' | 'on_hold' | 'dropped'
    note: string
    favorite: boolean
    updated_at: string
  }
}

export interface SeasonIndex {
  season_id: string
  work_id?: string
  season_number: number
  group_type: string
  label: string
  episode_count: number
  scrape_target_id?: string
  scrape_title?: string
  scrape_year?: number | null
  tmdb_id?: number | null
  tmdb_type?: string
  tmdb_season_number?: number | null
  nfo_path?: string
  poster_path?: string
  fanart_path?: string
  clearlogo_path?: string
  plot?: string
  rating?: number
  scraped?: boolean
}

export interface EpisodeIndex {
  episode_id: string
  work_id?: string
  source?: 'pan115' | 'baidu' | 'local' | 'openlist'
  provider_id?: ProviderId
  season_number: number
  episode_number: number
  special_number?: number | null
  title: string
  plot?: string
  runtime?: number
  group_type: string
  kind: string
  strm_path: string
  nfo_path?: string
  thumb_path?: string
  availability?: 'available' | 'missing' | 'source_unavailable'
  metadata_pending?: boolean
}

export interface TrackingBinding {
  binding_id: string
  work_id: string
  display_title: string
  logical_source: 'local' | 'pan115' | 'baidu'
  root_path: string
  season_number: number | null
  series_group: string
  tracking_state: 'tracking' | 'paused' | 'completed' | 'archived'
  attention_state: 'ready' | 'waiting_metadata' | 'waiting_review' | 'source_unavailable'
  last_snapshot_id: string
  baseline_plan_id: string
  last_scan_at: string
  last_successful_scan_at: string
  last_result: Record<string, unknown>
}

export interface RelatedWork {
  work_id: string
  title: string
  year: number | null
  card_type: string
  relation_type: string
  poster_path: string
  fanart_path: string
  show_type: string
}

// 播放历史
export interface PlaybackHistoryItem {
  history_id: string
  work_id: string
  work_title: string
  episode_id: string
  episode_title: string
  source: string
  media_type: string
  group_type: string
  season_number: number
  episode_number: number
  strm_path: string
  poster_path: string
  played_at: string
}

// 播放会话
export interface PlaybackSession {
  session_id: string
  work_id: string
  episode_id: string
  strm_path: string
  real_path: string
  pid: number
  status: 'playing' | 'stopped' | 'exited' | 'failed'
  started_at: string
  position: number
  duration: number
  ended_at: string | null
  exit_code: number | null
}

// 来源
export interface SourceInfo {
  source: string
  mirror_namespace: string
  available: boolean
}

export interface SourcePathValidation {
  source: string
  ok: boolean
  status: 'verified' | 'unavailable' | 'mismatch'
  configured_root: string
  resolved_root: string
  scope_name: string
  checked_count: number
  existing_count: number
  example_path: string
  message: string
}

// 导入预览
export interface ImportPreview {
  plan_id: string
  source: string
  status: 'draft' | 'confirmed' | 'executed'
  import_scope: '' | 'seasonal'
  summary: ImportSummary
  parse_logs: Array<{ kind: 'info' | 'done' | 'warn' | 'error'; message: string }>
  issues: PreviewIssue[]
  groups: PreviewGroup[]
  items: ImportPlanItem[]
}

export interface ImportSummary {
  total_items: number
  video_count: number
  generate_strm_count: number
  ignored_count: number
  attach_only_count: number
  low_confidence_count: number
  needs_review_count: number
  ungrouped_video_count: number
  duplicate_episode_count: number
  /** 按真实作品身份去重后的可生成镜像作品数，不等于 groups.length */
  work_count: number
}

export interface PreviewIssue {
  code: string
  level: 'error' | 'warning'
  message: string
  item_ids: string[]
}

export interface PreviewGroup {
  work_id: string
  work_title: string
  year: number | null
  card_type: string
  media_type: string
  show_type: string
  series_group: string
  group_type: string
  season_number: number | null
  item_count: number
  item_ids: string[]
  warnings: string[]
}

export interface ImportPlanItem {
  id: string
  plan_id: string
  raw_file_id: string
  source: string
  relative_path: string
  real_path: string
  resource_type: 'video' | 'subtitle' | 'nfo' | 'image' | 'font' | 'archive' | 'audio' | 'text' | 'other'
  action: 'generate_strm' | 'ignore' | 'attach_only'
  work_id: string
  work_title: string
  original_title: string
  year: number | null
  media_type: 'tv' | 'movie' | ''
  show_type: 'anime_series' | 'anime_movie' | 'live_series' | 'live_movie' | ''
  tmdb_hint_id?: number | null
  tmdb_hint_type?: string
  import_family?: 'anime' | 'live' | ''
  series_group: string
  card_type: 'main_series' | 'standalone' | ''
  belongs_to_series: string
  relation_type: 'main' | 'movie' | 'recap' | 'spin_off' | 'related' | ''
  group_type: 'season' | 'special' | 'sps' | 'op_ed' | 'movie' | 'ignored' | ''
  season_number: number | null
  episode_number: number | null
  special_number: number | null
  title: string
  target_dir: string
  target_filename: string
  target_strm_path: string
  confidence: 'high' | 'medium' | 'low'
  needs_review: boolean
  reasons: string[]
  warnings: string[]
  user_override_id: string | null
}

// 任务
export interface TaskRecord {
  task_id: string
  task_type: string
  source: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  message: string
  created_at: string
  started_at: string
  finished_at: string
  error: string
  result: unknown
}

// 导入解析结果
export interface SourceParseResult {
  snapshot_id: string
  plan_id: string
  source: ImportSourceId
  file_count: number
  video_count: number
  plan_status: string
  import_family?: string
}
