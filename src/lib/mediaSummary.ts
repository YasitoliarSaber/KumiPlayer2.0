/**
 * P-003：识别预览的纯聚合函数。
 *
 * 只做展示性投影，不参与身份判定，也不改写 MediaGraph。异常判定最终以后端
 * review issue 为准；集号跨度只做展示性提示，前端不得自行修改集号。
 */

import type { V4Preview, V4ResolvedEpisode, V4ReviewIssue } from '../api/mediaV4'

export interface WorkGroupSummary {
  kind: 'season' | 'special' | 'movie'
  seasonNumber: number | null
  episodeCount: number
  fileCount: number
  rangeLabel: string
  spanAnomaly: boolean
}

export interface WorkSummary {
  work_key: string
  title: string
  year: number | null
  media_type: 'tv' | 'movie'
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
  anomalyWorks: WorkSummary[]
  normalWorks: WorkSummary[]
}

function episodeKey(episode: V4ResolvedEpisode): string {
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
  if (numbers.length === 0) return ''
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
        episodeCount,
        fileCount,
        rangeLabel: fileCount > 0 ? `${fileCount} 个文件` : '电影',
        spanAnomaly: false,
      })
    } else {
      const bySeason = new Map<string, { seasonNumber: number | null; episodes: V4ResolvedEpisode[] }>()
      for (const episode of episodes) {
        const isSpecial = episode.season_kind === 'special' || episode.episode_kind === 'special' || episode.special_number != null
        const key = isSpecial ? 'special' : `season:${episode.local_season_number ?? 0}`
        const entry = bySeason.get(key) ?? { seasonNumber: isSpecial ? null : episode.local_season_number, episodes: [] }
        entry.episodes.push(episode)
        bySeason.set(key, entry)
      }
      for (const [key, entry] of bySeason) {
        const isSpecial = key === 'special'
        const unique = new Map<string, V4ResolvedEpisode>()
        for (const episode of entry.episodes) {
          unique.set(episodeKey(episode), episode)
        }
        const numbers = [...unique.values()].map((episode) =>
          isSpecial ? (episode.special_number ?? 0) : (episode.local_episode_number ?? 0),
        )
        const fileCount = entry.episodes.reduce((sum, episode) => sum + episode.asset_evidence_ids.length, 0)
        groups.push({
          kind: isSpecial ? 'special' : 'season',
          seasonNumber: isSpecial ? null : entry.seasonNumber,
          episodeCount: unique.size,
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
    works.push({
      work_key: work.work_key,
      title: work.preferred_title || '未命名作品',
      year: work.year,
      media_type: work.media_type === 'movie' ? 'movie' : 'tv',
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
