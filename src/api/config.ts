// KumiPlayer 2.0 配置 API

import { api } from './client';
import type { OpenListRoute } from './types';

export interface PublicConfig {
  setup_completed: boolean
  setup_version: number
  mpv_path: string
  server_port: number
  mirror_dir: string
  pan115_root: string
  baidu_root: string
  local_root: string
  directory_tree_dir: string
  openlist_server_url: string
  openlist_remote_root: string
  openlist_mount_root: string
  /** OpenList 用户名与密码是否已配置（凭据本身绝不回传前端） */
  openlist_configured: boolean
  /** KumiPlayer 本地浏览缓存时长（分钟） */
  openlist_cache_ttl_minutes: number
  /** 单层目录浏览的有界预取直接子目录数量上限 */
  openlist_prefetch_limit: number
  /** OpenList 提供商路由（远端前缀 -> provider） */
  openlist_routes: OpenListRoute[]
  tmdb_bearer_token: string
  tmdb_language: string
  tmdb_certification_regions: string
  artwork_storage_mode: 'remote' | 'local' | 'auto'
  anilist_enabled: boolean
  anilist_rate_limit: number
  anilist_timeout: number
  deepseek_api_key: string
  tmdb_rate_limit: number
  tmdb_max_retries: number
  tmdb_timeout: number
  bangumi_access_token: string
  bangumi_user_agent: string
  auto_play_next_episode: boolean
  mpv_anime4k_mode: 'off' | 'a' | 'b' | 'c' | 'a+a' | 'b+b' | 'c+a'
  mpv_anime4k_quality: 'light' | 'balanced' | 'high'
  series_card_image_mode: 'poster' | 'fanart'
  poster_size: number
  heartbeat_enabled: boolean
  heartbeat_timeout: number
  auto_shutdown_on_heartbeat_timeout: boolean
  proxy_url: string
}

export interface MpvRuntimeStatus {
  available: boolean
  version: string
  architecture: string
  target_triple: string
  manifest_valid: boolean
  files_valid: boolean
  configuration_available: boolean
  scripts_available: boolean
  distribution_status: string
  message: string
  runtime_dir: string
  config_dir: string
}

export interface MpvValidationResult {
  ok: boolean
  message: string
  version?: string
  integration_dir: string
  integration_available: boolean
  plugin_available: boolean
  plugin_path: string
  mpv_available?: boolean
  manifest_valid?: boolean
  files_valid?: boolean
  distribution_status?: string
}

export interface SetupCompletePayload {
  mpv_path?: string
  mirror_dir: string
  pan115_root?: string
  baidu_root?: string
  local_root?: string
  directory_tree_dir?: string
  tmdb_bearer_token?: string
  bangumi_access_token?: string
}

export interface MediaPathValidationItem {
  source: 'pan115' | 'baidu' | 'openlist'
  ok: boolean
  status: 'verified' | 'unavailable' | 'mismatch'
  configured_root: string
  resolved_root: string
  checked_count: number
  existing_count: number
  example_path: string
  message: string
}

export interface MediaPathValidationResponse {
  ok: boolean
  sources: MediaPathValidationItem[]
}

export const configApi = {
  getConfig: () => api.get<PublicConfig>('/api/config'),
  patchConfig: (patch: Partial<PublicConfig>) => api.patch<PublicConfig>('/api/config', patch),
  getMpvRuntime: () => api.get<MpvRuntimeStatus>('/api/config/mpv-runtime'),
  openMpvConfigDir: () => api.post<{ ok: boolean; config_dir: string }>('/api/config/mpv-runtime/open-config'),
  testMpv: () => api.post<{ ok: boolean; message: string }>('/api/config/test/mpv'),
  testMpvPath: (mpvPath: string) => api.post<MpvValidationResult>('/api/config/test/mpv-path', { mpv_path: mpvPath }),
  completeSetup: (payload: SetupCompletePayload) => api.post<PublicConfig>('/api/config/setup/complete', payload),
  testTmdb: () => api.post<{ ok: boolean; message: string }>('/api/config/test/tmdb'),
  testDeepseek: () => api.post<{ ok: boolean; message: string }>('/api/config/test/deepseek'),
  testMediaPaths: () => api.post<MediaPathValidationResponse>('/api/config/test/media-paths'),
}
