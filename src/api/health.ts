// KumiPlayer 2.0 健康检查 API

import { api } from './client'

export const healthApi = {
  check: () => api.get<{ status: string; app: string }>('/api/health'),
}
