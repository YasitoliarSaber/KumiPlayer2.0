// KumiPlayer 2.0 WebSocket 心跳

import { API_BASE } from './client'
import { withApiSessionToken } from './sessionToken'

export type HeartbeatStatus = 'connecting' | 'connected' | 'disconnected'

export function buildHeartbeatUrl(): string {
  if (API_BASE) {
    return withApiSessionToken(`${API_BASE.replace(/^http/, 'ws')}/ws/heartbeat`)
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return withApiSessionToken(`${protocol}//${window.location.host}/ws/heartbeat`)
}

export class HeartbeatClient {
  private ws: WebSocket | null = null
  private timer: number | null = null
  private reconnectTimer: number | null = null
  private _status: HeartbeatStatus = 'disconnected'
  private onStatusChange?: (status: HeartbeatStatus) => void
  private onAck?: () => void

  constructor(onStatusChange?: (status: HeartbeatStatus) => void, onAck?: () => void) {
    this.onStatusChange = onStatusChange
    this.onAck = onAck
  }

  get status(): HeartbeatStatus {
    return this._status
  }

  connect(): void {
    if (this.ws) return

    const wsUrl = buildHeartbeatUrl()
    this._setStatus('connecting')

    try {
      this.ws = new WebSocket(wsUrl)

      this.ws.onopen = () => {
        this._setStatus('connected')
        this.sendHeartbeat()
        this.startHeartbeat()
      }

      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'heartbeat_ack') {
            this.onAck?.()
          }
        } catch {
          // ignore
        }
      }

      this.ws.onclose = () => {
        this._setStatus('disconnected')
        this.cleanup()
        this.scheduleReconnect()
      }

      this.ws.onerror = () => {
        this.ws?.close()
      }
    } catch {
      this._setStatus('disconnected')
      this.scheduleReconnect()
    }
  }

  disconnect(): void {
    this.cleanup()
    this._setStatus('disconnected')
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private startHeartbeat(): void {
    this.timer = window.setInterval(() => {
      this.sendHeartbeat()
    }, 10_000)
  }

  private sendHeartbeat(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'heartbeat' }))
    }
  }

  private cleanup(): void {
    if (this.timer) {
      clearInterval(this.timer)
      this.timer = null
    }
    if (this.ws) {
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onclose = null
      this.ws.onerror = null
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.close()
      }
      this.ws = null
    }
  }

  private scheduleReconnect(): void {
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, 5000)
  }

  private _setStatus(status: HeartbeatStatus): void {
    this._status = status
    this.onStatusChange?.(status)
  }
}
