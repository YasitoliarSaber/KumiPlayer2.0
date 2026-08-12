// KumiPlayer 2.0 任务 API

import { api } from './client'
import type { TaskRecord } from './types'

export const tasksApi = {
  // 统一任务列表：镜像、刮削、媒体库重扫、来源解析都从这里看。
  list: (params?: {
    source?: string
    task_type?: string
    type_prefix?: string
    limit?: number
  }) => {
    const searchParams = new URLSearchParams()
    if (params?.source) searchParams.set('source', params.source)
    if (params?.task_type) searchParams.set('task_type', params.task_type)
    if (params?.type_prefix) searchParams.set('type_prefix', params.type_prefix)
    if (params?.limit) searchParams.set('limit', String(params.limit))
    const query = searchParams.toString()
    return api.get<{ tasks: TaskRecord[] }>(`/api/tasks${query ? `?${query}` : ''}`)
  },

  get: (taskId: string) =>
    api.get<TaskRecord>(`/api/tasks/${taskId}`),

  cancel: (taskId: string) =>
    api.post<TaskRecord>(`/api/tasks/${taskId}/cancel`),

  // 查询镜像任务
  getMirrorTask: (taskId: string) =>
    api.get<TaskRecord>(`/api/mirror/tasks/${taskId}`),

  // 查询刮削任务
  getScrapeTask: (taskId: string) =>
    api.get<TaskRecord>(`/api/scrape/tasks/${taskId}`),

  // 查询媒体库任务
  getLibraryTask: (taskId: string) =>
    api.get<TaskRecord>(`/api/library/tasks/${taskId}`),

  // 重扫媒体库
  rescanLibrary: (source?: string) =>
    api.post<{ task_id: string; status: string }>('/api/library/rescan', { source }),
}
