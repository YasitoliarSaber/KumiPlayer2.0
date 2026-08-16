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
  // V3（SQLite revision）返回 execution_mode='durable' + job_id：镜像任务已由
  // 后端入队，前端不再调用 mirrorApi.generate；legacy JSON 计划无这两个字段。
  confirm: (source: string, planId: string) =>
    api.post<{
      plan_id: string
      source: string
      status: string
      message: string
      execution_mode?: 'durable'
      job_id?: string
    }>(
      `/api/imports/${source}/confirm`,
      { plan_id: planId }
    ),

  // RWK-35：root 级批量确认（多作品 TXT baseline 一次确认全部 revisions）
  // RWK-38：root 级批量确认（多作品 TXT baseline 一次确认全部 revisions）
  // 确认身份 = (root_id, generation)——generation 由导入响应保存、确认时回传，
  // 后端做 generation fence（TOCTOU 防护：确认页内容与执行内容必须一致）。
  confirmRoot: (source: string, rootId: string, generation: number) =>
    api.post<{
      root_id: string
      generation: number
      source: string
      execution_mode: 'durable'
      confirmed_count: number
      revision_ids: string[]
      job_ids: string[]
    }>(
      `/api/imports/${source}/confirm-root`,
      { root_id: rootId, generation }
    ),

  // RWK-38（P1）：root-generation 聚合 durable preview（确认页唯一真相来源；
  // patch 后刷新此端点，不再读旧 legacy JSON preview）
  getConfirmRootPreview: (source: string, rootId: string, generation: number) =>
    api.get<ImportPreview & { revision_ids?: string[] }>(
      `/api/imports/${source}/confirm-root-preview?root_id=${encodeURIComponent(rootId)}&generation=${generation}`
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
