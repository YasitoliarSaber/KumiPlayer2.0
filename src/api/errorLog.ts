import { api, API_BASE } from './client'
import { apiSessionHeaders } from './sessionToken'

export interface ErrorLogEntry {
  id: string
  timestamp: string
  stage: string
  category: string
  level: string
  message: string
  source: string
  context: Record<string, unknown>
  resolved: boolean
}

export interface ErrorLogStats {
  total: number
  unresolved: number
  by_stage: Record<string, number>
  by_category: Record<string, number>
}

export async function getErrors(days = 7): Promise<{ errors: ErrorLogEntry[]; count: number }> {
  return api.get(`/api/error-log?days=${days}`)
}

export async function getErrorStats(): Promise<ErrorLogStats> {
  return api.get('/api/error-log/stats')
}

export async function resolveError(errorId: string): Promise<{ ok: boolean; error_id: string }> {
  return api.post('/api/error-log/resolve', { error_id: errorId })
}

export async function resolveAllErrors(
  stage?: string,
  category?: string,
): Promise<{ ok: boolean; resolved_count: number }> {
  return api.post('/api/error-log/resolve-all', { stage, category })
}

export async function deleteError(errorId: string): Promise<{ ok: boolean; error_id: string }> {
  return api.delete(`/api/error-log/${errorId}`)
}

export async function purgeErrors(params?: {
  source?: string
  stage?: string
  category?: string
}): Promise<{ ok: boolean; deleted_count: number }> {
  return api.post('/api/error-log/purge', params || {})
}

export async function exportErrorLogText(days = 90): Promise<string> {
  const response = await fetch(`${API_BASE}/api/error-log/export?days=${days}`, {
    headers: apiSessionHeaders(),
  })
  if (!response.ok) {
    throw new Error(`导出错误日志失败：HTTP ${response.status}`)
  }
  return response.text()
}
