import { describe, expect, it } from 'vitest'
import {
  recentEpisodeLabel,
  recentProgressLabel,
  recentTimeLabel,
} from '../../src/pages/RecentPage'
import type { PlaybackHistoryItem } from '../../src/api/types'

const NOW = new Date('2026-09-21T21:30:00')

function item(overrides: Partial<PlaybackHistoryItem> = {}): PlaybackHistoryItem {
  return {
    work_id: 'w1',
    episode_id: 'e1',
    asset_id: 'a1',
    position: 754,
    duration: 1440,
    completed: false,
    updated_at: '2026-09-21T21:20:00',
    season_number: 1,
    episode_number: 3,
    ...overrides,
  }
}

describe('「最近播放」标签（规格 §12）', () => {
  it('同时给出集号、进度与时间，而不是只显示作品', () => {
    const label = recentEpisodeLabel(item(), NOW)
    expect(label).toContain('第 3 集')
    expect(label).toContain('12:34 / 24:00')
    expect(label).toContain('52%')
    expect(label).toContain('今天 21:20')
    expect(label).not.toBe('最近播放')
  })

  it('缺集号与集标题时仍给出进度与时间（绝不退化成只有"最近播放"）', () => {
    const label = recentEpisodeLabel(
      item({ season_number: null, episode_number: null, episode_title: undefined }),
      NOW,
    )
    expect(label).toContain('12:34 / 24:00')
    expect(label).toContain('今天 21:20')
  })

  it('使用后端 /history 的快照字段（真实返回形状）', () => {
    const label = recentEpisodeLabel(
      item({
        season_number: null,
        episode_number: null,
        episode_title: undefined,
        season_snapshot: '第 1 季',
        episode_snapshot: '第 3 集',
      }),
      NOW,
    )
    expect(label).toContain('第 1 季 · 第 3 集')
    expect(label).toContain('12:34 / 24:00')
    expect(label).not.toBe('最近播放')
  })

  it('已看完与无进度分别给出明确文案', () => {
    expect(recentProgressLabel(item({ completed: true }))).toBe('已看完')
    expect(recentProgressLabel(item({ position: 0 }))).toBe('')
  })

  it('时间文案区分今天/昨天/更早', () => {
    expect(recentTimeLabel('2026-09-21T08:05:00', NOW)).toBe('今天 08:05')
    expect(recentTimeLabel('2026-09-20T23:10:00', NOW)).toBe('昨天 23:10')
    expect(recentTimeLabel('2026-09-01T09:00:00', NOW)).toBe('9 月 1 日 09:00')
  })
})
