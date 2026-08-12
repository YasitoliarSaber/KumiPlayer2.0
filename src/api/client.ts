// KumiPlayer 2.0 API Client

import { apiSessionHeaders } from './sessionToken'

// 开发模式通过 Vite proxy 走 /api；Tauri 生产页面与 FastAPI 不同源，
// 必须直连桌面壳启动的本机后端，不能把 /api 请求发给内嵌前端页面。
const API_BASE = import.meta.env.DEV
  ? (import.meta.env.VITE_API_BASE ?? '')
  : (import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:37821')

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function authorizedFetch(input: RequestInfo | URL, options: RequestInit = {}): Promise<Response> {
  return fetch(input, {
    ...options,
    headers: {
      ...apiSessionHeaders(),
      ...options.headers,
    },
  })
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${API_BASE}${path}`

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 30000)

  let response: Response
  const isForm = options.body instanceof FormData
  try {
    response = await authorizedFetch(url, {
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...options.headers,
    },
      ...options,
      signal: options.signal ?? controller.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(408, '请求超时，请重试')
    }
    if (error instanceof TypeError) {
      throw new ApiError(503, '无法连接 KumiPlayer 后端，请重试或使用恢复界面重启后端')
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
  }

  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const body = await response.json()
      message = body.detail || message
    } catch {
      // ignore
    }
    throw new ApiError(response.status, message)
  }

  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) {
    throw new ApiError(
      502,
      '后端返回了非 JSON 响应，请确认 KumiPlayer 后端已正常启动。',
    )
  }

  return response.json()
}

export const api = {
  get: <T>(path: string) => request<T>(path),

  post: <T>(path: string, body?: unknown, options: RequestInit = {}) =>
    request<T>(path, {
      ...options,
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  patch: <T>(path: string, body: unknown) =>
    request<T>(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  put: <T>(path: string, body: unknown) =>
    request<T>(path, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  delete: <T>(path: string) =>
    request<T>(path, { method: 'DELETE' }),

  form: <T>(path: string, method: 'POST' | 'PUT', body: FormData) =>
    request<T>(path, { method, body }),
}

export { API_BASE }
