// KumiPlayer 2.0 镜像 API

import { api } from './client'
import type { TaskRecord } from './types'

export const mirrorApi = {
  // 生成镜像
  generate: (source: string, planId?: string) =>
    api.post<{ task_id: string; status: string }>(
      `/api/mirror/${source}/generate`,
      planId ? { plan_id: planId } : undefined
    ),

  // 查询任务
  getTask: (taskId: string) =>
    api.get<TaskRecord>(`/api/mirror/tasks/${taskId}`),
}
