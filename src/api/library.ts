// KumiPlayer 2.0 媒体库 API

import { api } from './client'
import type { WorkIndex } from './types'

export interface DeletePreviewFile {
  path: string
  kind: string
  exists: boolean
  allowed: boolean
  reason: string
}

export interface DeletePreviewResponse {
  preview_id: string
  source: string
  scope: string
  work_id: string
  files: DeletePreviewFile[]
  empty_dirs: string[]
  warnings: string[]
  blocked: boolean
  retained_work_ids: string[]
  library_work_count: number
  media_preset_count: number
  tracking_binding_count: number
  tracking_scan_run_count: number
  history_count: number
  progress_count: number
  related_reference_count: number
}

export interface DeleteConfirmResponse {
  preview_id: string
  status: 'succeeded' | 'partial_failed' | 'failed'
  deleted: string[]
  failed: Array<{ path: string; reason: string }>
  skipped: Array<{ path: string; reason: string }>
  empty_dirs_removed: string[]
  library_rescanned: boolean
  deleted_library_work_count: number
  deleted_preset_ids: string[]
  deleted_tracking_binding_count: number
  deleted_tracking_scan_run_count: number
  cancelled_tracking_task_count: number
}

export type LibraryDeleteSource = 'all' | 'pan115' | 'baidu' | 'local' | 'openlist'
export type LocalWatchStatusValue = '' | 'watching' | 'watched' | 'on_hold' | 'dropped'

export interface LibraryDiagnosticItem {
  code: string
  message: string
  source?: string
  library_work_id?: string
  scrape_target_id?: string
  series_group?: string
  scrape_title?: string
  local_season_number?: number | null
  tmdb_id?: number | null
  tmdb_season_number?: number | null
  path?: string
}

export interface LibraryDiagnosticsResponse {
  ok: boolean
  summary: {
    work_count: number
    scrape_item_count: number
    checked_scrape_seasons: number
    error_count: number
    warning_count: number
  }
  errors: LibraryDiagnosticItem[]
  warnings: LibraryDiagnosticItem[]
}

export interface LocalWatchStatus {
  work_id: string
  status: LocalWatchStatusValue
  note: string
  favorite: boolean
  updated_at: string
}

export const libraryApi = {
  // 获取媒体库
  getLibrary: (params?: {
    source?: string
    compact?: boolean
  }) => {
    const searchParams = new URLSearchParams()
    if (params?.source && params.source !== 'all') searchParams.set('source', params.source)
    if (params?.compact) searchParams.set('compact', 'true')

    const query = searchParams.toString()
    return api.get<{ works: WorkIndex[]; summary: Record<string, unknown>; generated_at: string; needs_rescan: boolean }>(
      `/api/library${query ? `?${query}` : ''}`
    )
  },

  // 获取作品详情
  getWorkDetail: (workId: string) =>
    api.get<WorkIndex>(`/api/library/works/${workId}`),

  setWorkTitle: (workId: string, title: string) =>
    api.patch<{ work_id: string; title: string; provenance: 'manual' }>(
      `/api/library/works/${workId}/title`,
      { title },
    ),

  restoreWorkTitle: (workId: string) =>
    api.delete<{ work_id: string; restored: boolean }>(`/api/library/works/${workId}/title`),

  deleteWorkPreview: (workId: string) =>
    api.post<DeletePreviewResponse>(`/api/library/works/${workId}/delete/preview`),

  deleteWorkConfirm: (workId: string, previewId: string) =>
    api.post<DeleteConfirmResponse>(`/api/library/works/${workId}/delete/confirm`, {
      preview_id: previewId,
    }),

  // 一键清空生成媒体库预览：只删除 mirror root 内生成文件，不碰源文件。
  deleteLibraryPreview: (source: LibraryDeleteSource = 'all') =>
    api.post<DeletePreviewResponse>('/api/library/delete/library/preview', {
      source,
    }),

  // 一键清空生成媒体库确认。
  deleteLibraryConfirm: (previewId: string) =>
    api.post<DeleteConfirmResponse>(
      '/api/library/delete/library/confirm',
      { preview_id: previewId }
    ),

  // 重扫媒体库（简化版）
  rescanLibrary: async (source?: string) => {
    const result = await api.post<{ task_id: string; status: string }>('/api/library/rescan', { source });
    return result;
  },

  // 获取诊断信息
  getDiagnostics: async (source?: string) => {
    const params = source ? `?source=${encodeURIComponent(source)}` : '';
    return api.get<LibraryDiagnosticsResponse>(`/api/library/diagnostics${params}`);
  },

  setWatchStatus: (workId: string, status: LocalWatchStatusValue, note = '', favorite?: boolean) =>
    api.patch<LocalWatchStatus>(`/api/library/watch-status/${workId}`, { status, note, favorite }),
}
