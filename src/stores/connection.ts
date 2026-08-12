import { create } from 'zustand'
import { healthApi } from '../api/health'
import { HeartbeatClient, type HeartbeatStatus } from '../api/heartbeat'
import { useLibraryStore } from './library'

interface ConnectionState {
  backendOnline: boolean
  heartbeatStatus: HeartbeatStatus
  checking: boolean

  checkHealth: () => Promise<void>
  startHeartbeat: () => void
  stopHeartbeat: () => void
  startHealthPolling: () => void
  stopHealthPolling: () => void
}

let heartbeatClient: HeartbeatClient | null = null
let healthTimer: ReturnType<typeof setInterval> | null = null

export const useConnectionStore = create<ConnectionState>((set, get) => ({
  backendOnline: false,
  heartbeatStatus: 'disconnected',
  checking: false,

  checkHealth: async () => {
    set({ checking: true })
    try {
      await healthApi.check()
      const wasOffline = !get().backendOnline
      set({ backendOnline: true, checking: false })
      if (wasOffline) {
        const lib = useLibraryStore.getState()
        if (!lib.loaded && !lib.loading) {
          await lib.loadLibrary()
        }
      }
    } catch {
      set({ backendOnline: false, checking: false })
    }
  },

  startHeartbeat: () => {
    if (heartbeatClient) return
    heartbeatClient = new HeartbeatClient(
      (status) => set({ heartbeatStatus: status }),
      undefined
    )
    heartbeatClient.connect()
  },

  stopHeartbeat: () => {
    heartbeatClient?.disconnect()
    heartbeatClient = null
  },

  startHealthPolling: () => {
    if (healthTimer) return
    healthTimer = setInterval(() => {
      get().checkHealth()
    }, 30_000)
  },

  stopHealthPolling: () => {
    if (healthTimer) {
      clearInterval(healthTimer)
      healthTimer = null
    }
  },
}))