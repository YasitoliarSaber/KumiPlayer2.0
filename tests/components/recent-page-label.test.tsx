import { describe, expect, it } from 'vitest'
import {
  recentEpisodeLabel,
  recentRemainingLabel,
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

describe('「最近观看」标签（继续播放入口，不是播放日志）', () => {
  it('第二层给集号与剩余时间，第三层给时间；不输出精确位置/总时长/百分比', () => {
    const label = recentEpisodeLabel(item(), NOW)
    expect(label).toContain('S01E03')
    expect(label).toContain('还剩 12 分钟')
    expect(label).toContain('今天 21:20')
    expect(label).not.toContain('最近播放')
    // 调试式文案必须彻底消失：不同时以文本展示位置、总时长与百分比。
    expect(label).not.toMatch(/\d+:\d+\s*\/\s*\d+:\d+/)
    expect(label).not.toContain('%')
  })

  it('缺集号与集标题时仍给出剩余时间与时间（绝不退化成只有"最近播放"）', () => {
    const label = recentEpisodeLabel(
      item({ season_number: null, episode_number: null, episode_title: undefined }),
      NOW,
    )
    expect(label).toContain('还剩 12 分钟')
    expect(label).toContain('今天 21:20')
    expect(label).not.toContain('%')
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
    expect(label).toContain('S01E03')
    expect(label).toContain('还剩 12 分钟')
    expect(label).not.toBe('最近播放')
  })

  it('已看完、长片与缺少总时长分别给出明确文案', () => {
    expect(recentRemainingLabel(item({ completed: true }))).toBe('已看完 ✓')
    // 电影 2:00:08 看了 46:20 → 1 小时 14 分（向上取整）。
    expect(recentRemainingLabel(item({ position: 2780, duration: 7208 }))).toBe('还剩 1 小时 14 分')
    // 剩余不足一小时只给分钟。
    expect(recentRemainingLabel(item({ position: 100, duration: 1500 }))).toBe('还剩 24 分钟')
    // 没有总时长时不给任何精确数字。
    expect(recentRemainingLabel(item({ duration: 0 }))).toBe('继续观看')
    expect(recentRemainingLabel(item({ position: 0, duration: 0 }))).toBe('继续观看')
  })

  it('保留未知编号与第零季，不补成第一集', () => {
    expect(recentEpisodeLabel(item({ season_number: 0, episode_number: 6 }), NOW)).toContain('S00E06')
    expect(recentEpisodeLabel(item({ season_number: null, episode_number: 6 }), NOW)).toContain('E06')
    const unknown = recentEpisodeLabel(item({ season_number: null, episode_number: null }), NOW)
    expect(unknown).not.toContain('S01E01')
    expect(unknown).toContain('集号待定')
  })

  it('时间文案区分今天/昨天/更早', () => {
    expect(recentTimeLabel('2026-09-21T08:05:00', NOW)).toBe('今天 08:05')
    expect(recentTimeLabel('2026-09-20T23:10:00', NOW)).toBe('昨天 23:10')
    expect(recentTimeLabel('2026-09-01T09:00:00', NOW)).toBe('9 月 1 日 09:00')
  })
})
