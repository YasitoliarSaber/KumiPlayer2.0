/** P-003 识别摘要纯聚合函数合同：基数受控、层级正确、异常可见。 */

import { describe, expect, test } from 'vitest'
import type { V4Preview } from '../../src/api/mediaV4'
import { buildWorkSummaries, sortWorkUnits } from '../../src/lib/mediaSummary'

function makeEpisode(workKey: string, index: number, extra: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    work_key: workKey,
    episode_key: `${workKey}-${index}`,
    local_season_number: 1,
    local_episode_number: index,
    absolute_episode_number: null,
    season_kind: 'regular',
    episode_kind: 'regular',
    special_number: null,
    edition_key: 'default',
    asset_evidence_ids: [`asset-${workKey}-${index}`],
    ...extra,
  }
}

describe('buildWorkSummaries', () => {
  test('500+ 集只聚合层级摘要，不逐集展开', () => {
    const preview: V4Preview = {
      revision_id: 'rev-big',
      status: 'draft',
      works: [{ work_key: 'w1', preferred_title: '大长篇', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
      episodes: Array.from({ length: 520 }, (_, index) => makeEpisode('w1', index + 1) as never),
      work_assets: [],
      issues: [],
    }
    const summary = buildWorkSummaries(preview)
    expect(summary.totalWorks).toBe(1)
    expect(summary.totalFiles).toBe(520)
    expect(summary.works[0].groups).toHaveLength(1)
    expect(summary.works[0].groups[0].episodeCount).toBe(520)
    // 默认折叠：聚合结果不携带任何逐集标签。
    expect(JSON.stringify(summary)).not.toContain('S01E520')
  })

  test('集号跨度异常被展示性标记', () => {
    const preview: V4Preview = {
      revision_id: 'rev-anomaly',
      status: 'draft',
      works: [{ work_key: 'w1', preferred_title: '跨度作品', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
      episodes: [
        makeEpisode('w1', 1),
        makeEpisode('w1', 520),
      ] as never,
      work_assets: [],
      issues: [],
    }
    const summary = buildWorkSummaries(preview)
    expect(summary.anomalyWorks).toHaveLength(1)
    expect(summary.anomalyWorks[0].groups[0].spanAnomaly).toBe(true)
  })

  test('电影、两季与特别篇分层正确，S00 不作为普通季', () => {
    const preview: V4Preview = {
      revision_id: 'rev-layers',
      status: 'draft',
      works: [
        { work_key: 'series', preferred_title: '两季动画', year: 2023, media_type: 'tv', source_evidence_ids: [] },
        { work_key: 'movie', preferred_title: '剧场版', year: 2024, media_type: 'movie', source_evidence_ids: [] },
      ],
      episodes: [
        makeEpisode('series', 1),
        makeEpisode('series', 2),
        makeEpisode('series', 1, { local_season_number: 2 }),
        makeEpisode('series', 1, { season_kind: 'special', special_number: 1 }),
      ] as never,
      work_assets: [{ work_key: 'movie', edition_key: 'default', asset_evidence_ids: ['asset-movie-1'] }],
      issues: [],
    }
    const summary = buildWorkSummaries(preview)
    const series = summary.works.find((work) => work.work_key === 'series')
    expect(series?.groups.map((group) => group.kind).sort()).toEqual(['season', 'season', 'special'])
    expect(series?.groups[0].rangeLabel).toBe('E01–E02')
    expect(summary.totalFiles).toBe(5) // 4 集 + 1 电影文件
  })

  test('issue 作品置顶且不被普通列表淹没', () => {
    const preview: V4Preview = {
      revision_id: 'rev-issue',
      status: 'draft',
      works: [
        { work_key: 'bad', preferred_title: '问题作品', year: 2024, media_type: 'tv', source_evidence_ids: ['ev-bad'] },
        { work_key: 'good', preferred_title: '正常作品', year: 2024, media_type: 'tv', source_evidence_ids: [] },
      ],
      episodes: [makeEpisode('bad', 1), makeEpisode('good', 1)] as never,
      work_assets: [],
      issues: [{ code: 'unresolved_title', evidence_id: 'ev-bad', message: '标题未识别' }],
    }
    const summary = buildWorkSummaries(preview)
    expect(summary.totalIssues).toBe(1)
    expect(summary.anomalyWorks.map((work) => work.work_key)).toEqual(['bad'])
    expect(summary.normalWorks.map((work) => work.work_key)).toEqual(['good'])
  })
})

describe('sortWorkUnits', () => {
  test('运行中 > 需要处理 > 等待 > 完成', () => {
    const units = [
      { work_id: 'done', title: '完成', overall_status: 'completed' },
      { work_id: 'run', title: '运行', overall_status: 'running_mirror' },
      { work_id: 'wait', title: '等待', overall_status: 'waiting_metadata' },
      { work_id: 'fail', title: '失败', overall_status: 'failed' },
    ]
    const sorted = sortWorkUnits(units).map((unit) => unit.overall_status)
    expect(sorted).toEqual(['running_mirror', 'failed', 'waiting_metadata', 'completed'])
  })
})

import { isSeasonalWork } from '../../src/utils/libraryCategories'
import type { WorkIndex } from '../../src/api/types'

describe('isSeasonalWork', () => {
  test('基于后端 watch_status，不再常量 false', () => {
    const base = { work_id: 'w', title: 'x', original_title: '', year: null, rating: 0, plot: '', genres: [], studios: [], show_type: 'anime_series' as const, media_type: 'tv' as const, source: 'pan115' as const, card_type: 'main_series' as const, poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '' } as WorkIndex
    expect(isSeasonalWork({ ...base, watch_status: { work_id: 'w', status: 'watching', note: '', favorite: false, updated_at: '' } })).toBe(true)
    expect(isSeasonalWork({ ...base, watch_status: { work_id: 'w', status: '', note: '', favorite: true, updated_at: '' } })).toBe(false)
    expect(isSeasonalWork(base)).toBe(false)
  })
})


test('S00 特别篇不作为普通季度', () => {
  const preview: V4Preview = {
    revision_id: 'rev-s00', status: 'draft',
    works: [{ work_key: 'w1', preferred_title: '正片', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
    episodes: [
      makeEpisode('w1', 1, { local_season_number: 1, season_kind: 'regular' }),
      makeEpisode('w1', 1, { local_season_number: 0, season_kind: 'special', special_number: 1 }),
    ] as never,
    work_assets: [],
    issues: [],
  }
  const summary = buildWorkSummaries(preview)
  const kinds = summary.works[0].groups.map((group) => group.kind)
  expect(kinds).toEqual(['season', 'special'])
  expect(summary.works[0].groups.find((group) => group.kind === 'special')?.seasonNumber).toBeNull()
})

describe('未知值的显示闭环（STEP-014）', () => {
  test('unknown 媒体类型保留为 unknown，不冒充 TV 剧集', () => {
    const preview: V4Preview = {
      revision_id: 'rev-unknown-type', status: 'draft',
      works: [{ work_key: 'u1', preferred_title: '未定位作品', year: null, media_type: 'unknown', source_evidence_ids: [] }],
      episodes: [makeEpisode('u1', 1, { local_season_number: null, local_episode_number: 8 }) as never],
      work_assets: [], issues: [],
    }
    const summary = buildWorkSummaries(preview)
    expect(summary.works[0].media_type).toBe('unknown')
    expect(summary.unknownTypeWorks).toBe(1)
    // 季号未知仍然标出，且不被当成第 0 季。
    expect(summary.works[0].groups[0].seasonUnknown).toBe(true)
    expect(summary.works[0].groups[0].seasonNumber).toBeNull()
    expect(summary.works[0].groups[0].rangeLabel).toBe('E08')
  })

  test('季号 null 与显式第 0 季分成两组，不互相吞并', () => {
    const preview: V4Preview = {
      revision_id: 'rev-season-null-zero', status: 'draft',
      works: [{ work_key: 's1', preferred_title: '混合季号', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
      episodes: [
        makeEpisode('s1', 1, { local_season_number: null, local_episode_number: 1 }),
        makeEpisode('s1', 1, { local_season_number: 0, season_kind: 'special', special_number: 1 }),
      ] as never,
      work_assets: [], issues: [],
    }
    const summary = buildWorkSummaries(preview)
    const groups = summary.works[0].groups
    expect(groups).toHaveLength(2)
    const unknownSeason = groups.find((group) => group.seasonUnknown)
    const seasonZero = groups.find((group) => group.kind === 'special')
    expect(unknownSeason).toBeDefined()
    expect(seasonZero).toBeDefined()
    // 未知季不显示成第 0 季；显式 0 归特别篇，两者不合并成一组。
    expect(unknownSeason?.seasonNumber).toBeNull()
    expect(unknownSeason?.seasonUnknown).toBe(true)
    expect(seasonZero?.seasonNumber).toBeNull()
    expect(seasonZero?.seasonUnknown).toBe(false)
  })

  test('集号 null 不生成 E00，也不被算进范围端点', () => {
    const preview: V4Preview = {
      revision_id: 'rev-episode-null', status: 'draft',
      works: [{ work_key: 'e1', preferred_title: '部分未编号', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
      episodes: [
        makeEpisode('e1', 1, { identity_key: 'e1|s1|e1' }),
        // 同一逻辑集的第二个版本：共享 identity_key，只应算一集。
        makeEpisode('e1', 2, { identity_key: 'e1|s1|e2', edition_key: 'alt' }),
        makeEpisode('e1', 2, { identity_key: 'e1|s1|e2' }),
        makeEpisode('e1', 3, { local_episode_number: null, identity_key: 'e1|s1|unassigned-1' }),
      ] as never,
      work_assets: [], issues: [],
    }
    const summary = buildWorkSummaries(preview)
    const group = summary.works[0].groups[0]
    expect(group.rangeLabel).toBe('E01–E02')
    expect(group.rangeLabel).not.toContain('E00')
    expect(group.episodeCount).toBe(3)
    expect(group.unresolvedEpisodeCount).toBe(1)
    expect(summary.totalUnresolvedEpisodes).toBe(1)
  })

  test('全部集号未知时只写集号未定，不补缺集也不编造范围', () => {
    const preview: V4Preview = {
      revision_id: 'rev-all-null', status: 'draft',
      works: [{ work_key: 'n1', preferred_title: '全未编号', year: 2024, media_type: 'unknown', source_evidence_ids: [] }],
      episodes: [
        makeEpisode('n1', 1, { local_season_number: null, local_episode_number: null, identity_key: 'n1|unassigned-a' }),
        makeEpisode('n1', 2, { local_season_number: null, local_episode_number: null, identity_key: 'n1|unassigned-b' }),
      ] as never,
      work_assets: [], issues: [],
    }
    const summary = buildWorkSummaries(preview)
    const group = summary.works[0].groups[0]
    expect(group.rangeLabel).toBe('集号未定')
    expect(group.unresolvedEpisodeCount).toBe(2)
    expect(JSON.stringify(summary)).not.toContain('E00')
  })

  test('电影无集号仍按电影展示，不要求集号', () => {
    const preview: V4Preview = {
      revision_id: 'rev-movie', status: 'draft',
      works: [{ work_key: 'm1', preferred_title: '剧场版', year: 2024, media_type: 'movie', source_evidence_ids: [] }],
      episodes: [],
      work_assets: [{ work_key: 'm1', edition_key: 'default', asset_evidence_ids: ['asset-m1'] }],
      issues: [],
    }
    const summary = buildWorkSummaries(preview)
    const work = summary.works[0]
    expect(work.media_type).toBe('movie')
    expect(work.groups[0].kind).toBe('movie')
    expect(work.groups[0].rangeLabel).toBe('1 个文件')
    expect(work.groups[0].unresolvedEpisodeCount).toBe(0)
    expect(summary.totalUnresolvedEpisodes).toBe(0)
    expect(summary.unknownTypeWorks).toBe(0)
  })
})
