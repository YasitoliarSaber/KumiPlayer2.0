// KumiPlayer 2.0 任务 API

import { api } from './client'
import type { TaskRecord } from './types'

export const tasksApi = {
  // V4 jobs 表的唯一任务查询入口。
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

  retry: (taskId: string) =>
    api.post<TaskRecord>(`/api/tasks/${taskId}/retry`),

  rescanLibrary: () => api.post<{ task_id: string; status: string }>('/api/library/rescan'),
}
