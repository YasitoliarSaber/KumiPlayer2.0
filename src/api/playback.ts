// KumiPlayer 2.0 播放 API

import { api } from './client'
import type { PlaybackHistoryItem, PlaybackSession } from './types'

export const playbackApi = {
  // 播放
  play: (params: {
    work_id: string
    episode_id?: string
    strm_path?: string
  }) =>
    api.post<{ session_id: string; status: string }>('/api/playback/play', params),

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
    return api.get<{ items: PlaybackHistoryItem[]; total: number }>(
      `/api/playback/history${query ? `?${query}` : ''}`
    )
  },

  // 继续播放
  getContinue: (workId: string) =>
    api.get<{ episode_id: string; strm_path: string; season_number: number; episode_number: number } | null>(
      `/api/playback/continue/${workId}`
    ),

  getProgress: (workId?: string) =>
    api.get<{ items: Array<{
      work_id: string
      episode_id: string
      position: number
      duration: number
      ratio: number
      completed: boolean
      updated_at: string
      bangumi_synced: boolean
      bangumi_error: string
      manually_unwatched: boolean
    }> }>(`/api/playback/progress${workId ? `?work_id=${encodeURIComponent(workId)}` : ''}`),

  reportProgress: (params: { work_id: string; episode_id: string; position: number; duration: number }) =>
    api.post<{
      work_id: string
      episode_id: string
      position: number
      duration: number
      ratio: number
      completed: boolean
      updated_at: string
      bangumi_synced: boolean
      bangumi_error: string
      manually_unwatched: boolean
    }>('/api/playback/progress', params),

  markProgress: (params: { work_id: string; episode_id: string; completed: boolean }) =>
    api.post<{
      work_id: string
      episode_id: string
      position: number
      duration: number
      ratio: number
      completed: boolean
      updated_at: string
      bangumi_synced: boolean
      bangumi_error: string
      manually_unwatched: boolean
    }>('/api/playback/progress/mark', params),
}
