// KumiPlayer 2.0 播放 API

import { api } from './client'
import type { PlaybackHistoryItem, PlaybackSession } from './types'

export type PlaybackProgressRequest = {
  work_id: string
  episode_id: string
  asset_id?: string
  position: number
  duration: number
  completed?: boolean
}

export const playbackApi = {
  // 播放
  play: (params: {
    work_id: string
    episode_id: string
    asset_id?: string
  }) =>
    api.post<{
      session_id: string
      status: string
      work_id: string
      episode_id: string
      asset_id: string
      playback_locator: string
    }>('/api/playback/play', params),

  // 停止
  stop: () =>
    api.post<{ status: string; session_id?: string }>('/api/playback/stop'),

  // 状态
  getStatus: () =>
    api.get<{ status: string; session: PlaybackSession | null }>('/api/playback/status'),

  // 历史
  getHistory: (params?: number | { limit?: number; work_id?: string }) => {
    const searchParams = new URLSearchParams()
    if (typeof params === 'number') {
      searchParams.set('limit', String(params))
    } else if (params) {
      if (params.limit) searchParams.set('limit', String(params.limit))
      if (params.work_id) searchParams.set('work_id', params.work_id)
    }
    const query = searchParams.toString()
    return api.get<{ items: PlaybackHistoryItem[] }>(
      `/api/playback/history${query ? `?${query}` : ''}`
    )
  },

  getProgress: (workId?: string) =>
    api.get<{ items: PlaybackHistoryItem[] }>(
      `/api/playback/progress${workId ? `?work_id=${encodeURIComponent(workId)}` : ''}`,
    ),

  reportProgress: (params: PlaybackProgressRequest) =>
    api.post<PlaybackHistoryItem>('/api/playback/progress', params),

  markProgress: (params: { work_id: string; episode_id: string; asset_id?: string; completed: boolean }) =>
    api.post<PlaybackHistoryItem>('/api/playback/progress/mark', params),
}
