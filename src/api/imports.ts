// KumiPlayer 2.0 导入 API

import { api } from './client'
import type { ImportPreview, ImportPlanItem } from './types'

export const importsApi = {
  // 获取预览
  getPreview: (source: string, planId?: string) =>
    api.get<ImportPreview>(
      `/api/imports/${source}/preview${planId ? `?plan_id=${planId}` : ''}`
    ),

  // 修正条目
  patchItem: (source: string, itemId: string, planId: string, patch: Record<string, unknown>) =>
    api.patch<{ item: ImportPlanItem; summary: Record<string, unknown> }>(
      `/api/imports/${source}/items/${itemId}`,
      { plan_id: planId, patch }
    ),

  // 确认计划
  confirm: (source: string, planId: string) =>
    api.post<{ plan_id: string; source: string; status: string; message: string }>(
      `/api/imports/${source}/confirm`,
      { plan_id: planId }
    ),

  // 生成 diff
  createDiff: (source: string, params?: { old_snapshot_id?: string; new_snapshot_id?: string }) =>
    api.post<{
      diff_id: string
      source: string
      old_snapshot_id?: string
      new_snapshot_id?: string
      old_video_count?: number
      new_video_count?: number
      added_count: number
      missing_count: number
      moved_count?: number
      renamed_count?: number
      unchanged_count?: number
      uncertain_count?: number
      safety: {
        blocked: boolean
        delete_ratio?: number
        path_change_ratio?: number
        total_change_ratio?: number
        reasons: string[]
      }
    }>(`/api/imports/${source}/diff`, params),

  // 增量预览
  incrementalPreview: (source: string, params?: { old_snapshot_id?: string; new_snapshot_id?: string }) =>
    api.post<ImportPreview>(`/api/imports/${source}/incremental/preview`, params),
}
