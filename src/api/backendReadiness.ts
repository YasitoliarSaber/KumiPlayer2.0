import { apiSessionHeaders } from './sessionToken.ts'

const DESKTOP_CONFIG_URL = 'http://127.0.0.1:37821/api/config'
const DEFAULT_ATTEMPTS = 120
const DEFAULT_INTERVAL_MS = 250

interface BackendHealthResponse {
  ok: boolean
  json: () => Promise<unknown>
}

interface BackendReadinessOptions {
  attempts?: number
  fetchHealth?: () => Promise<BackendHealthResponse>
  delay?: (milliseconds: number) => Promise<void>
}

const defaultDelay = (milliseconds: number) => new Promise<void>((resolve) => {
  window.setTimeout(resolve, milliseconds)
})

const defaultFetchHealth = () => fetch(DESKTOP_CONFIG_URL, {
  cache: 'no-store',
  headers: apiSessionHeaders(),
})

export async function waitForDesktopBackend({
  attempts = DEFAULT_ATTEMPTS,
  fetchHealth = defaultFetchHealth,
  delay = defaultDelay,
}: BackendReadinessOptions = {}): Promise<void> {
  const totalAttempts = Math.max(1, attempts)

  for (let attempt = 0; attempt < totalAttempts; attempt += 1) {
    try {
      const response = await fetchHealth()
      if (response.ok) {
        const body = await response.json() as { setup_completed?: unknown }
        if (typeof body.setup_completed === 'boolean') return
      }
    } catch {
      // Cold starts briefly reject connections until Uvicorn finishes initialization.
    }

    if (attempt + 1 < totalAttempts) {
      await delay(DEFAULT_INTERVAL_MS)
    }
  }

  throw new Error('KumiPlayer 后端在 30 秒内未能就绪')
}
