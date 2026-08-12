import { invoke, isTauri } from '@tauri-apps/api/core'

import { waitForDesktopBackend } from './backendReadiness'
import { setApiSessionToken } from './sessionToken'


export async function initializeDesktopApiSession(): Promise<void> {
  if (!isTauri()) {
    setApiSessionToken(import.meta.env.VITE_API_TOKEN ?? '')
    return
  }

  const token = await invoke<string>('get_api_token')
  if (!token) throw new Error('桌面安全会话初始化失败')
  setApiSessionToken(token)
  await waitForDesktopBackend()
}
