// KumiPlayer 2.0 OpenList 连接 API（单实例连接 + 提供商路由 + 懒加载浏览 + 批量导入）

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
  // 兼容字段：不再代表 1000 截断语义，新后端可能不再返回
  truncated?: boolean
  refresh_requested?: boolean
  cache: OpenListCacheMeta
}

export interface OpenListTestResult {
  ok: boolean
  message: string
  insecure_http_required?: boolean
}

export interface OpenListSaveResult {
  ok: boolean
  message: string
}

export interface OpenListTaskResult {
  task_id: string
  task_status: string
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

export interface OpenListBatchImportPayload {
  remote_paths: string[]
  import_family: string
  import_scope?: string
}

// 后台导入批次：POST /api/openlist/import-batch 创建的持久批次。
// 每个 root 及其识别单元独立推进，不回流旧版确认计划工作台。
export interface OpenListImportBatchRoot {
  batch_id: string
  root_id: string
  remote_locator: string
  normalized_locator: string
  local_locator: string
  import_family: string
  import_scope: string
  status: string
  generation: number
  job_id: string
  job_status?: string
  progress?: number
  message?: string
  error?: string
  plan_ids?: string[]
  units?: BackgroundImportUnit[]
}

export interface BackgroundImportJob {
  job_id: string
  status: string
  progress: number
  message: string
  error: string
  result: Record<string, unknown>
}

export interface BackgroundImportUnit {
  unit_id: string
  revision_id: string
  work_title: string
  boundary: string
  video_count: number
  discovery_status: string
  revision_status?: string
  state: string
  error?: string
  mirror_job?: BackgroundImportJob
  scrape_job?: BackgroundImportJob
  library_rebuild_job?: BackgroundImportJob
}

export interface OpenListImportBatch {
  batch_id: string
  status: string
  mode: string
  import_family: string
  created_at: string
  updated_at: string
  roots: OpenListImportBatchRoot[]
  job_ids: string[]
}

export const openlistApi = {
  testConnection: (payload: Partial<OpenListConfigPayload>) =>
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
  // V2 durable batch 链路（import-batch → discovery job → SQLite revision）
  createImportBatch: (payload: OpenListBatchImportPayload) =>
    api.post<OpenListImportBatch>('/api/openlist/import-batch', payload),
  getImportBatch: (batchId: string) =>
    api.get<OpenListImportBatch>(`/api/openlist/import-batches/${batchId}`),
  cancelImportBatch: (batchId: string) =>
    api.post<OpenListImportBatch>(`/api/openlist/import-batches/${batchId}/cancel`, {}),
  rescanPreset: (presetId: string) =>
    api.post<OpenListTaskResult>(`/api/openlist/presets/${presetId}/rescan`, {}),
}
