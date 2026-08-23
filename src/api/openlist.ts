// KumiPlayer 2.0 OpenList 配置、路由与懒加载浏览 API。

import { api } from './client'
import type { OpenListRoute, ProviderId } from './types'

export interface OpenListEntry {
  name: string
  is_dir: boolean
  size: number | null
  modified: number | null
  remote_path: string
}

export interface OpenListCacheMeta {
  cached: boolean
  status: 'fresh' | 'stale' | 'none'
  refreshing: boolean
  refresh_failed: boolean
  error?: string
  fetched_at: number | null
  expires_at: number | null
}

// browse 契约（模块4 C3）：一次请求只拉一页（page >= 1, 1 <= per_page <= 100）。
// total > 0 表示总数已知（has_more = page*per_page < total）；total = 0 表示未知（has_more = len(entries) == per_page）。
export interface OpenListBrowseResult {
  path: string
  parent_path: string | null
  remote_root: string
  entries: OpenListEntry[]
  page: number
  per_page: number
  total: number
  has_more: boolean
  // 服务端无法获知总数时为 0；此字段仅表达当前页是否还有后续内容。
  truncated?: boolean
  refresh_requested?: boolean
  cache: OpenListCacheMeta
}
//: 连接测试请求契约：候选配置（KEEP SAVED / 显式新值语义由后端解析）
export interface OpenListTestConnectionPayload {
  server_url: string
  remote_root: string
  username: string
  password: string
  allow_insecure_http?: boolean
}

export interface OpenListTestResult {
  ok: boolean
  code: string
  phase: string
  message: string
  insecure_http_required?: boolean
}

export interface OpenListSaveResult {
  ok: boolean
  message: string
}

/** HYB-6：KumiPlayer → OpenList 请求成本遥测摘要 */
export interface OpenListTelemetrySummary {
  fs_list: number
  login: number
  total: number
  disclaimer: string
}


export interface OpenListConfigPayload {
  server_url: string
  remote_root: string
  mount_root: string
  username: string
  password: string
  allow_insecure_http?: boolean
  cache_ttl_minutes?: number
  prefetch_limit?: number
  /** 仅保存不验证：跳过 Fresh Probe，直接持久化凭据 */
  skip_verification?: boolean
}

export interface OpenListRoutesResult {
  routes: OpenListRoute[]
}

export interface OpenListRouteItem {
  route_id: string
  label: string
  remote_prefix: string
  provider_id: ProviderId
  enabled: boolean
}

export interface OpenListDiscoverItem {
  name: string
  remote_prefix: string
  hint_provider: ProviderId
  current_provider: ProviderId | ''
  current_label: string
}

export interface OpenListDiscoverResult {
  remote_root: string
  items: OpenListDiscoverItem[]
}

export interface OpenListPrefetchResult {
  prefetched: number
  skipped: number
  busy: boolean
}

export const openlistApi = {
  testConnection: (payload: OpenListTestConnectionPayload) =>
    api.post<OpenListTestResult>('/api/openlist/test-connection', payload),
  saveConfig: (payload: OpenListConfigPayload) =>
    api.post<OpenListSaveResult>('/api/openlist/config', payload),
  browse: (path = '', page = 1, refresh = false, perPage = 100) =>
    api.get<OpenListBrowseResult>(
      `/api/openlist/browse?path=${encodeURIComponent(path)}&page=${page}&per_page=${perPage}&refresh=${refresh ? 'true' : 'false'}`,
    ),
  prefetch: (paths: string[]) =>
    api.post<OpenListPrefetchResult>('/api/openlist/prefetch', { paths }),
  getRoutes: () => api.get<OpenListRoutesResult>('/api/openlist/routes'),
  discoverRoutes: () => api.post<OpenListDiscoverResult>('/api/openlist/routes/discover', {}),
  saveRoutes: (routes: OpenListRouteItem[]) =>
    api.put<OpenListRoutesResult>('/api/openlist/routes', { routes }),
  // HYB-6：今日 KumiPlayer → OpenList 请求成本遥测（只读）
  getTelemetryToday: () =>
    api.get<OpenListTelemetrySummary>('/api/openlist/telemetry/today'),
}
