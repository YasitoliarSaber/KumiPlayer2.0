/**
 * P-003：识别预览的纯聚合函数。
 *
 * 只做展示性投影，不参与身份判定，也不改写 MediaGraph。异常判定最终以后端
 * review issue 为准；集号跨度只做展示性提示，前端不得自行修改集号。
 */

import type { V4ExecutionProgress, V4Preview, V4ResolvedEpisode, V4ReviewIssue } from '../api/mediaV4'

/** 全页面共用的作品口径；阶段 pill 的分子分母另为后台任务数。 */
export function summarizeExecutionWorks(progress: V4ExecutionProgress) {
  const units = progress.work_units
  const defectCodes = new Set(['episode_mapping_incomplete', 'special_episode_metadata_incomplete', 'artifact_incomplete'])
  return {
    total: units.length,
    completed: units.filter((unit) => unit.overall_status === 'completed').length,
    needsAttention: units.filter((unit) => unit.overall_status === 'needs_attention' || unit.overall_status === 'failed').length,
    metadataComplete: units.filter((unit) => unit.metadata_state === 'ready' && !defectCodes.has(unit.metadata_reason_code ?? '')).length,
  }
}

export interface WorkGroupSummary {
  kind: 'season' | 'special' | 'movie'
  seasonNumber: number | null
  /**
   * 季号未知（`local_season_number` 为 `null`）。显示为「季号未定」，
   * 绝不映射为第 0 季或第 1 季；显式 `0` 仍是第 0 季，两者不合并。
   */
  seasonUnknown: boolean
  episodeCount: number
  /**
   * 集号未知（`local_episode_number` 为 `null`）的条目数。与已知集号范围分开
   * 展示，不参与范围端点，也不补齐缺集。
   */
  unresolvedEpisodeCount: number
  /** 本组是否有可用数值集号（电影组为 false：它按文件数展示）。 */
  hasNumberedItems: boolean
  fileCount: number
  rangeLabel: string
  spanAnomaly: boolean
}

export interface WorkSummary {
  work_key: string
  title: string
  year: number | null
  /** 保留后端事实：`unknown` 是真实状态，不得当成 tv 展示。 */
  media_type: 'tv' | 'movie' | 'unknown'
  episodeCount: number
  assetCount: number
  groups: WorkGroupSummary[]
  issues: V4ReviewIssue[]
  hasAnomaly: boolean
}

export interface RecognitionSummary {
  works: WorkSummary[]
  totalWorks: number
  totalVideos: number
  totalFiles: number
  totalIssues: number
  /** 媒体类型仍未确定的作品数（不是 0，也不是 tv）。 */
  unknownTypeWorks: number
  /** 集号仍未确定的条目总数（不是缺集，也不是 E00）。 */
  totalUnresolvedEpisodes: number
  anomalyWorks: WorkSummary[]
  normalWorks: WorkSummary[]
}

function episodeKey(episode: V4ResolvedEpisode): string {
  // 逻辑集身份优先：同一集的多个版本共享 `identity_key`，只算一集；
  // 两个都未编号的不同条目各有自己的 `identity_key`，不会被合并成一个。
  // 旧后端不返回时回退到「季 + 集 + 类型」组合（与既行为一致）。
  const identity = episode.identity_key
  if (identity) return `${episode.work_key}:${identity}`
  return `${episode.work_key}:${episode.local_season_number ?? 's'}:${episode.local_episode_number ?? 'e'}:${episode.season_kind}:${episode.special_number ?? 'x'}`
}

function spanAnomaly(episodeNumbers: number[]): boolean {
  if (episodeNumbers.length < 2) return false
  const sorted = [...episodeNumbers].sort((a, b) => a - b)
  const span = sorted[sorted.length - 1] - sorted[0] + 1
  // 展示性提示：跨度远超数量（例如 520 集跨度只有少数条目）才标异常，
  // 防止把正常长季误报；真正的身份问题以后端 issue 为准。
  return span > Math.max(12, episodeNumbers.length * 2)
}

function rangeLabelFor(kind: WorkGroupSummary['kind'], numbers: number[]): string {
  // 没有可用集号就是「集号未定」：不得用 0 当占位，也不得拼出 E00。
  if (numbers.length === 0) return '集号未定'
  if (kind === 'movie') return `${numbers.length} 个文件`
  const sorted = [...numbers].sort((a, b) => a - b)
  const prefix = kind === 'special' ? 'SP' : 'E'
  if (numbers.length === 1) return `${prefix}${String(sorted[0]).padStart(2, '0')}`
  return `${prefix}${String(sorted[0]).padStart(2, '0')}–${prefix}${String(sorted[sorted.length - 1]).padStart(2, '0')}`
}

export function buildWorkSummaries(preview: V4Preview): RecognitionSummary {
  const issuesByEvidence = new Map<string, V4ReviewIssue[]>()
  for (const issue of preview.issues) {
    const list = issuesByEvidence.get(issue.evidence_id) ?? []
    list.push(issue)
    issuesByEvidence.set(issue.evidence_id, list)
  }
  const issuesByWork = new Map<string, V4ReviewIssue[]>()
  for (const work of preview.works) {
    const issues: V4ReviewIssue[] = []
    for (const evidenceId of work.source_evidence_ids) {
      issues.push(...(issuesByEvidence.get(evidenceId) ?? []))
    }
    issuesByWork.set(work.work_key, issues)
  }

  const works: WorkSummary[] = []
  let totalFiles = 0
  for (const work of preview.works) {
    const episodes = preview.episodes.filter((episode) => episode.work_key === work.work_key)
    const movieAssets = preview.work_assets.filter((asset) => asset.work_key === work.work_key)
    const groups: WorkGroupSummary[] = []

    if (work.media_type === 'movie') {
      const fileCount = movieAssets.reduce((sum, asset) => sum + asset.asset_evidence_ids.length, 0)
      const movieEpisodes = episodes.filter((episode) => episode.season_kind === 'movie' || episode.episode_kind === 'movie')
      const episodeCount = new Set(movieEpisodes.map(episodeKey)).size
      groups.push({
        kind: 'movie',
        seasonNumber: null,
        seasonUnknown: false,
        episodeCount,
        unresolvedEpisodeCount: 0,
        hasNumberedItems: false,
        fileCount,
        rangeLabel: fileCount > 0 ? `${fileCount} 个文件` : '电影',
        spanAnomaly: false,
      })
    } else {
      const bySeason = new Map<string, { seasonNumber: number | null; seasonUnknown: boolean; episodes: V4ResolvedEpisode[] }>()
      for (const episode of episodes) {
        const isSpecial = episode.season_kind === 'special' || episode.episode_kind === 'special' || episode.special_number != null
        // 季号 null 与显式第 0 季是两个不同的组，不能合并成一个「第 0 季」。
        const seasonUnknown = episode.local_season_number === null
        const key = isSpecial ? 'special' : `season:${seasonUnknown ? 'unknown' : episode.local_season_number}`
        const entry = bySeason.get(key) ?? { seasonNumber: isSpecial || seasonUnknown ? null : episode.local_season_number, seasonUnknown, episodes: [] }
        entry.episodes.push(episode)
        bySeason.set(key, entry)
      }
      for (const [key, entry] of bySeason) {
        const isSpecial = key === 'special'
        const unique = new Map<string, V4ResolvedEpisode>()
        for (const episode of entry.episodes) {
          unique.set(episodeKey(episode), episode)
        }
        // 只有有数值的集号参与排序与范围；未知项单独计数。
        const values = [...unique.values()].map((episode) =>
          isSpecial ? episode.special_number : episode.local_episode_number,
        )
        const numbers = values.filter((value): value is number => typeof value === 'number')
        const unresolvedEpisodeCount = values.length - numbers.length
        const fileCount = entry.episodes.reduce((sum, episode) => sum + episode.asset_evidence_ids.length, 0)
        groups.push({
          kind: isSpecial ? 'special' : 'season',
          seasonNumber: isSpecial || entry.seasonUnknown ? null : entry.seasonNumber,
          seasonUnknown: isSpecial ? false : entry.seasonUnknown,
          episodeCount: unique.size,
          unresolvedEpisodeCount,
          hasNumberedItems: numbers.length > 0,
          fileCount,
          rangeLabel: rangeLabelFor(isSpecial ? 'special' : 'season', numbers),
          spanAnomaly: spanAnomaly(numbers),
        })
      }
    }

    const issues = issuesByWork.get(work.work_key) ?? []
    const workFiles = episodes.reduce((sum, episode) => sum + episode.asset_evidence_ids.length, 0)
      + movieAssets.reduce((sum, asset) => sum + asset.asset_evidence_ids.length, 0)
    totalFiles += workFiles
    const hasAnomaly = issues.length > 0 || groups.some((group) => group.spanAnomaly)
    // 类型与集号都属于真实事实：unknown 保持 unknown，不当作 tv，也不补成第 1 季。
    const mediaType = work.media_type === 'movie'
      ? 'movie' as const
      : work.media_type === 'unknown'
        ? 'unknown' as const
        : 'tv' as const
    works.push({
      work_key: work.work_key,
      title: work.preferred_title || '未命名作品',
      year: work.year,
      media_type: mediaType,
      episodeCount: episodes.length,
      assetCount: workFiles,
      groups,
      issues,
      hasAnomaly,
    })
  }

  const anomalyWorks = works.filter((work) => work.hasAnomaly)
  const normalWorks = works.filter((work) => !work.hasAnomaly)
  return {
    works,
    totalWorks: works.length,
    totalVideos: works.reduce((sum, work) => sum + work.episodeCount, 0),
    totalFiles,
    totalIssues: preview.issues.length,
    unknownTypeWorks: works.filter((work) => work.media_type === 'unknown').length,
    totalUnresolvedEpisodes: works.reduce(
      (sum, work) => sum + work.groups.reduce((groupSum, group) => groupSum + group.unresolvedEpisodeCount, 0),
      0,
    ),
    anomalyWorks,
    normalWorks,
  }
}

export const WORK_PROGRESS_LABELS: Record<string, string> = {
  waiting_mirror: '等待生成镜像',
  running_mirror: '正在生成镜像',
  waiting_metadata: '等待获取媒体信息',
  running_metadata: '正在获取媒体信息',
  needs_attention: '需要处理',
  failed: '失败',
  cancelled: '已取消',
  completed: '已完成',
}

export const WORK_PROGRESS_ORDER: Record<string, number> = {
  running_mirror: 0,
  running_metadata: 0,
  needs_attention: 1,
  failed: 1,
  cancelled: 2,
  waiting_mirror: 3,
  waiting_metadata: 3,
  completed: 4,
}

export const STAGE_LABELS: Record<string, string> = {
  mirror: '生成镜像文件',
  metadata: '获取媒体信息',
  projection: '更新媒体库',
}

export function sortWorkUnits<T extends { overall_status: string }>(units: T[]): T[] {
  return [...units].sort((a, b) => {
    const orderDiff = (WORK_PROGRESS_ORDER[a.overall_status] ?? 5) - (WORK_PROGRESS_ORDER[b.overall_status] ?? 5)
    if (orderDiff !== 0) return orderDiff
    return a.overall_status.localeCompare(b.overall_status) || 'title' in a
      ? String((a as { title?: string }).title ?? '').localeCompare(String((b as { title?: string }).title ?? ''), 'zh')
      : 0
  })
}
