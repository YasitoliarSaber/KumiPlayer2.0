// KumiPlayer 2.0 API 类型定义

// V4 来源入口与内容提供商。
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
  card_type: 'main_series' | 'standalone'
  poster_path: string
  fanart_path: string
  local_poster_path?: string
  local_fanart_path?: string
  local_clearlogo_path?: string
  clearlogo_path: string
  dir_path: string
  seasons: SeasonIndex[]
  episodes: EpisodeIndex[]
  related_works: RelatedWork[]
  cast?: Array<{ name: string; role: string; profile_path: string }>
  tags: string[]
  last_played: string | null
  metadata_state?: 'ready' | 'waiting_metadata' | 'waiting_review' | 'source_unavailable' | 'failed'
  episode_count?: number
  asset_count?: number
  source_locations?: Record<string, string[]>
  main_episode_count?: number
  latest_episode_number?: number
  /** P-001 阶段6：常规季数与特别篇数由后端权威投影提供。 */
  season_count?: number
  special_season_count?: number
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
  title?: string
}

export interface EpisodeIndex {
  episode_id: string
  work_id?: string
  source?: 'pan115' | 'baidu' | 'local' | 'openlist'
  provider_id?: ProviderId
  season_number: number
  episode_number: number | null
  special_number?: number | null
  title: string
  plot?: string
  runtime?: number
  group_type: string
  kind: string
  playback_locator?: string
  asset_id?: string
  assets?: Array<{
    asset_id: string
    playback_locator?: string
    availability?: 'available' | 'missing' | 'source_unavailable'
    source?: 'pan115' | 'baidu' | 'local' | 'openlist'
  }>
  thumb_path?: string
  availability?: 'available' | 'missing' | 'source_unavailable'
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

// V4 播放进度/历史读模型。
// 历史接口直接返回 playback_progress；剧集和 Asset 身份由 ID 关联到媒体图。
export interface PlaybackHistoryItem {
  work_id: string
  episode_id: string
  asset_id: string
  position: number
  duration: number
  completed: boolean
  updated_at: string
  // 详情页可选地补充这些展示字段；它们不是播放状态事实。
  episode_title?: string
  season_number?: number | null
  episode_number?: number | null
}

// V4 播放适配器状态。当前桌面播放器会话不由后端持久化。
export interface PlaybackSession {
  session_id: string
  work_id: string
  episode_id: string
  asset_id: string
  playback_locator: string
  status: string
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
