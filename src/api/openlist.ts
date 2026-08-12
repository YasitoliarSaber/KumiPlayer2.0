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

export interface OpenListBrowseResult {
  path: string
  parent_path: string | null
  remote_root: string
  entries: OpenListEntry[]
  total: number
  truncated: boolean
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

// V2 durable batch：POST /api/openlist/import-batch 创建的持久批次。
// root.job_status 为 succeeded 时，plan_ids 为可进入确认页的 revision_id 列表
// （前端逐个调 /api/imports/openlist/preview?plan_id= 读取确认计划）。
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
  browse: (path = '', page = 1, refresh = false) =>
    api.get<OpenListBrowseResult>(
      `/api/openlist/browse?path=${encodeURIComponent(path)}&page=${page}&refresh=${refresh ? 'true' : 'false'}`,
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
  importRemote: (remotePath: string, importFamily: string, importScope = '') =>
    api.post<OpenListTaskResult>('/api/openlist/import', {
      remote_path: remotePath,
      import_family: importFamily,
      import_scope: importScope,
    }),
  batchImport: (payload: OpenListBatchImportPayload) =>
    api.post<OpenListTaskResult>('/api/openlist/batch-import', payload),
  rescanPreset: (presetId: string) =>
    api.post<OpenListTaskResult>(`/api/openlist/presets/${presetId}/rescan`, {}),
}
