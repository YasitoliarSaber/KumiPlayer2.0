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

export interface OpenListTaskResult {
  task_id: string
  task_status: string
}

/** HYB-2：TXT 目录树安全初始化（bootstrap）结果 */
export interface OpenListBootstrapResult {
  task_id: string
  root_id: string
  generation: number
  preset_id: string
  execution_mode: string
  scan_channel: string
  scan_mode: string
  resolution: string
  requested_locator: string
  canonical_locator: string
  tree_file_count: number
  tree_video_count: number
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
  // 重叠导入解析结果（import-batch 创建响应一次性提示）：
  // created | exact_reused | covered_by_existing_root | promoted_to_parent
  resolution?: string
  requested_locator?: string
  canonical_locator?: string
  covered_root_ids?: string[]
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
  // import-batch 同步创建/复用的来源媒体库卡（OpenList SourceRoot 长期入口）
  presets?: {
    preset_id: string
    name: string
    remote_locator: string
    catalog_root_id: string
    created: boolean
  }[]
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
  // V2 durable batch 链路（import-batch → discovery job → SQLite revision）
  createImportBatch: (payload: OpenListBatchImportPayload) =>
    api.post<OpenListImportBatch>('/api/openlist/import-batch', payload),
  getImportBatch: (batchId: string) =>
    api.get<OpenListImportBatch>(`/api/openlist/import-batches/${batchId}`),
  cancelImportBatch: (batchId: string) =>
    api.post<OpenListImportBatch>(`/api/openlist/import-batches/${batchId}/cancel`, {}),
  retryImportUnit: (batchId: string, unitId: string) =>
    api.post<OpenListImportBatch & { retried_stages?: Record<string, string> }>(
      `/api/openlist/import-batches/${batchId}/units/${unitId}/retry`, {}),
  rescanPreset: (presetId: string) =>
    api.post<OpenListTaskResult>(`/api/openlist/presets/${presetId}/rescan`, {}),
  // HYB-2/RWK-11：目录树 TXT 安全初始化（Provider 身份，零 OpenList 请求）。
  // provider：pan115 / baidu；remoteLocator 可选（纯 TXT 模式不要求）。
  bootstrapTree: (params: {
    provider?: 'pan115' | 'baidu'
    remoteLocator?: string
    localMountRoot: string
    importFamily?: string
    importScope?: string
    treeFile: File
  }) => {
    const body = new FormData()
    if (params.provider) body.append('provider', params.provider)
    if (params.remoteLocator) body.append('remote_locator', params.remoteLocator)
    body.append('local_mount_root', params.localMountRoot)
    if (params.importFamily) body.append('import_family', params.importFamily)
    if (params.importScope) body.append('import_scope', params.importScope)
    body.append('tree_file', params.treeFile)
    return api.form<OpenListBootstrapResult>('/api/openlist/bootstrap-tree', 'POST', body)
  },
  // RWK-3：给已存在的 Provider root 绑定可选 OpenList 增量通道
  bindRoot: (rootId: string, remoteLocator: string) =>
    api.post<{ root_id: string; bound: boolean; openlist_remote_locator: string; source_id: string }>(
      '/api/openlist/bind-root',
      { root_id: rootId, remote_locator: remoteLocator },
    ),
  // RWK-10：Bound Provider root 的真实 durable OpenList 增量扫描入口
  rescanBoundRoot: (rootId: string) =>
    api.post<OpenListTaskResult & { scan_channel: string; scan_mode: string }>(
      '/api/openlist/bound-roots/rescan',
      { root_id: rootId },
    ),
  // HYB-6：今日 KumiPlayer → OpenList 请求成本遥测（只读）
  getTelemetryToday: () =>
    api.get<OpenListTelemetrySummary>('/api/openlist/telemetry/today'),
}
