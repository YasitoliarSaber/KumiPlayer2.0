/**
 * P-003 第三步：V4 执行进度 = 总体阶段 + 作品单元。
 *
 * 消费后端 execution progress DTO（V4ExecutionProgress）。每部作品一个进度框，
 * 状态由 mirror + metadata jobs 组合推导；完成项默认折叠。原始 job_id 只作为
 * 重试命令参数，普通界面不展示 UUID 文本。
 */

import { useEffect, useRef, useState } from 'react'
import { Button, ProgressBar, Spinner } from '@fluentui/react-components'
import { CheckmarkCircle24Filled, ChevronDown24Regular, ChevronRight24Regular, DismissCircle24Regular, ErrorCircle24Regular, SpinnerIosRegular, Warning24Regular } from '@fluentui/react-icons'
import type { V4ExecutionProgress, V4WorkExecutionDetail, V4WorkProgressUnit } from '../../api/mediaV4'
import { STAGE_LABELS, WORK_PROGRESS_LABELS, sortWorkUnits } from '../../lib/mediaSummary'

export interface V4ExecutionProgressProps {
  progress: V4ExecutionProgress
  busyRetryId: string
  onRetry: (jobId: string) => void
  resolvingWorkId: string
  onResolveMetadata: (workId: string) => void
  fetchWorkDetail?: (revisionId: string, workId: string) => Promise<V4WorkExecutionDetail>
}

const STAGE_KEYS = ['mirror', 'metadata', 'projection'] as const
const COMPLETED_PREVIEW_COUNT = 4
const EXECUTION_STATUS_LABELS: Record<V4ExecutionProgress['overall_status'], string> = {
  queued: '正在准备任务',
  running: '正在建立媒体库',
  needs_attention: '有任务需要处理',
  completed: '媒体库已建立',
  cancelled: '任务已终止',
}

function StageSummary({ stageKey, progress }: { stageKey: (typeof STAGE_KEYS)[number]; progress: V4ExecutionProgress }) {
  const summary = progress.stage_summary[stageKey]
  const done = summary.succeeded + summary.failed + summary.cancelled
  return (
    <div className={`media-v4-stage-pill media-v4-stage-${summary.status}`}>
      <strong>{STAGE_LABELS[stageKey]}</strong>
      <span>{summary.total > 0 ? `${done}/${summary.total}` : '未开始'}</span>
    </div>
  )
}

type WorkDetailState =
  | { status: 'loading' }
  | { status: 'loaded'; detail: V4WorkExecutionDetail }
  | { status: 'error'; message: string }

function seasonLabel(season: V4WorkExecutionDetail['seasons'][number]): string {
  if (season.season_kind === 'special') return '特别篇'
  return season.season_number > 0 ? `第 ${season.season_number} 季` : (season.title || '未分季')
}

function detailRuntimeLabel(runtime: number | null | undefined): string {
  return runtime != null && runtime > 0 ? `${runtime} 分钟` : ''
}

function candidateDecisionLabel(decision: string): string {
  if (decision === 'auto_adopted') return '自动采用'
  if (decision === 'trusted_binding') return '沿用已确认身份'
  if (decision === 'waiting_review') return '等待确认'
  return decision || '未记录候选决策'
}

function WorkUnit({ unit, getWorkDetail, requestWorkDetail, busyRetryId, onRetry, resolvingWorkId, onResolveMetadata }: {
  unit: V4WorkProgressUnit
  getWorkDetail: (workId: string, cacheKey: string) => WorkDetailState | undefined
  requestWorkDetail: (workId: string, cacheKey: string) => void
  busyRetryId: string
  onRetry: (jobId: string) => void
  resolvingWorkId: string
  onResolveMetadata: (workId: string) => void
}) {
  // 失败项保留简洁的摘要，避免错误堆满长列表；真正需要用户确认身份的
  // needs_attention 则直接展开，确保“选择正确作品”的恢复入口不会被藏住。
  const [expanded, setExpanded] = useState(unit.overall_status === 'needs_attention')
  useEffect(() => {
    if (unit.overall_status === 'needs_attention') setExpanded(true)
  }, [unit.overall_status])
  const failedJob = unit.overall_status === 'failed'
    ? (unit.mirror.status === 'failed' ? unit.mirror : unit.metadata.status === 'failed' ? unit.metadata : null)
    : null
  const workIsPending = ['waiting_mirror', 'running_mirror', 'waiting_metadata', 'running_metadata'].includes(unit.overall_status)
  // 3.1：展开非进行中作品时按需读取执行详情；缓存键包含任务状态，
  // 重新执行该作品后状态变化自然使缓存失效并重新读取。
  const detailCacheKey = `${unit.mirror.status}:${unit.metadata.status}`
  const detail = !workIsPending && expanded ? getWorkDetail(unit.work_id, detailCacheKey) : undefined
  // 请求触发放在 effect：渲染期间只读取缓存，不修改父组件状态。
  useEffect(() => {
    if (expanded && !workIsPending) requestWorkDetail(unit.work_id, detailCacheKey)
  }, [expanded, workIsPending, unit.work_id, detailCacheKey, requestWorkDetail])
  const mirrorStatusLabel: Record<string, string> = {
    succeeded: '镜像已完成', failed: '镜像失败', running: '正在生成镜像', queued: '等待生成镜像', cancelled: '镜像已取消',
  }
  const metadataStateLabels: Record<string, string> = {
    ready: '媒体信息已就绪', waiting_review: '需要人工确认作品', waiting_metadata: '缺少在线资料配置',
    source_unavailable: '在线资料服务暂不可用', failed: '获取媒体信息失败',
  }
  const loadedDetail = detail?.status === 'loaded' ? detail.detail : null
  // 保持对旧后端/旧缓存响应的兼容：新增详情字段缺失时仍显示已有结果，
  // 不能因为一条旧响应让整个作品卡片渲染崩溃。
  const detailSeasons = loadedDetail?.seasons ?? []
  const detailEpisodes = loadedDetail?.episodes ?? []
  const scrape = loadedDetail?.scrape
  const scrapeHasContent = Boolean(scrape && (
    scrape.title
    || scrape.original_title
    || scrape.plot
    || scrape.candidate_decision
    || scrape.year != null
    || scrape.rating != null
    || scrape.runtime != null
    || scrape.premiered
    || (scrape.genres?.length ?? 0) > 0
    || (scrape.studios?.length ?? 0) > 0
  ))
  const detailHasContent = Boolean(loadedDetail && (
    loadedDetail.has_detail
    || detailSeasons.length > 0
    || detailEpisodes.length > 0
    || scrapeHasContent
  ))
  const orderedSeasons = [...detailSeasons].sort((left, right) => {
      const leftSpecial = left.season_kind === 'special'
      const rightSpecial = right.season_kind === 'special'
      if (leftSpecial !== rightSpecial) return leftSpecial ? 1 : -1
      return left.season_number - right.season_number
    })
  const detailPanelId = `media-v4-work-detail-${unit.work_id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
  return (
    <article className={`media-v4-work-progress media-v4-work-progress-${unit.overall_status}`}>
      <button type="button" className="media-v4-work-progress-head" aria-expanded={expanded} aria-controls={detailPanelId} onClick={() => setExpanded((value) => !value)}>
        <span className="media-v4-work-progress-state" aria-hidden="true">
          {unit.overall_status === 'completed' ? <CheckmarkCircle24Filled />
            : unit.overall_status === 'failed' ? <ErrorCircle24Regular />
            : unit.overall_status === 'cancelled' ? <DismissCircle24Regular />
            : unit.overall_status === 'needs_attention' ? <Warning24Regular />
            : <SpinnerIosRegular />}
        </span>
        <span className="media-v4-work-progress-title">{unit.title}</span>
        {/* 主行只保留一句可扫读的数量：文件数与集数一致时不再重复。 */}
        <span className="media-v4-work-progress-meta">
          {unit.media_type === 'movie' ? '电影' : '剧集'}
          {unit.episode_count > 0 ? ` · ${unit.episode_count} 集` : ''}
          {unit.asset_count !== unit.episode_count ? ` · ${unit.asset_count} 个文件` : ''}
        </span>
        <span className="media-v4-work-progress-status">{WORK_PROGRESS_LABELS[unit.overall_status] ?? unit.overall_status}</span>
        {expanded ? <ChevronDown24Regular aria-hidden="true" /> : <ChevronRight24Regular aria-hidden="true" />}
      </button>
      {expanded && (
        <div id={detailPanelId} className="media-v4-work-progress-body" role="region" aria-label={`${unit.title}执行详情`}>
          {failedJob?.last_error && <div className="media-v4-job-error" role="alert">{failedJob.last_error}</div>}
          {failedJob && (
            <Button size="small" appearance="secondary" disabled={busyRetryId !== ''} onClick={() => onRetry(failedJob.job_id)}>
              {busyRetryId === failedJob.job_id ? '正在重试…' : '重试'}
            </Button>
          )}
          {unit.overall_status === 'needs_attention' && <>
            <div className="media-v4-job-error" role="status">
              {unit.metadata_reason || '在线媒体信息没有唯一匹配，需要确认正确作品后继续。'}
            </div>
            {unit.metadata_state === 'waiting_review' && (
              <Button size="small" appearance="secondary" disabled={resolvingWorkId !== ''} onClick={() => onResolveMetadata(unit.work_id)}>
                {resolvingWorkId === unit.work_id ? '正在查找候选…' : '选择正确作品'}
              </Button>
            )}
          </>}
          {!failedJob && workIsPending && <span className="media-v4-work-progress-hint">任务进行中，完成后自动折叠到“已完成”。</span>}
          {detail?.status === 'loading' && <div className="media-v4-work-detail-loading"><Spinner size="tiny" />正在读取执行详情…</div>}
          {detail?.status === 'error' && (
            <div className="media-v4-job-error" role="alert">执行详情读取失败：{detail.message}</div>
          )}
          {detail?.status === 'loaded' && !detailHasContent && (
            <div className="media-v4-work-detail-empty">本次任务未生成详细结果。</div>
          )}
          {detail?.status === 'loaded' && detailHasContent && (
            <div className="media-v4-work-detail">
              <div className="media-v4-work-detail-section">
                <h4>作品信息</h4>
                <div className="media-v4-work-detail-facts">
                  <span>{detail.detail.work.title}</span>
                  <span>{detail.detail.work.provider === 'tmdb' ? `TMDB ${detail.detail.work.provider_id}` : detail.detail.work.provider || '未关联在线作品'}</span>
                  <span>{metadataStateLabels[detail.detail.work.metadata_state] ?? detail.detail.work.metadata_state}</span>
                </div>
                {detail.detail.work.metadata_reason && <div className="media-v4-job-error" role="status">{detail.detail.work.metadata_reason}</div>}
              </div>
              {scrapeHasContent && scrape && (
                <div className="media-v4-work-detail-section">
                  <h4>刮削结果</h4>
                  <div className="media-v4-work-detail-facts">
                    {scrape.title && <span>刮削标题：{scrape.title}</span>}
                    {scrape.original_title && <span>原文标题：{scrape.original_title}</span>}
                    {scrape.year != null && scrape.year > 0 && <span>{scrape.year} 年</span>}
                    {scrape.rating != null && <span>评分 {scrape.rating.toFixed(1)}</span>}
                    {detailRuntimeLabel(scrape.runtime) && <span>{detailRuntimeLabel(scrape.runtime)}</span>}
                    {scrape.premiered && <span>首播 {scrape.premiered}</span>}
                    {scrape.metadata_state && <span>{metadataStateLabels[scrape.metadata_state] ?? scrape.metadata_state}</span>}
                  </div>
                  {scrape.plot && <p className="media-v4-work-detail-plot">{scrape.plot}</p>}
                  {(scrape.genres?.length ?? 0) > 0 || (scrape.studios?.length ?? 0) > 0 ? (
                    <div className="media-v4-work-detail-facts">
                      {(scrape.genres?.length ?? 0) > 0 && <span>类型：{scrape.genres.join('、')}</span>}
                      {(scrape.studios?.length ?? 0) > 0 && <span>制作：{scrape.studios.join('、')}</span>}
                    </div>
                  ) : null}
                  {scrape.candidate_decision && (
                    <div className="media-v4-work-detail-candidate">
                      <div className="media-v4-work-detail-facts">
                        <span>
                          {candidateDecisionLabel(scrape.candidate_decision.decision)}
                          {scrape.candidate_decision.selected_score != null
                            ? ` · 匹配度 ${Math.round(scrape.candidate_decision.selected_score)}`
                            : ''}
                        </span>
                        {scrape.candidate_decision.reason && <span>{scrape.candidate_decision.reason}</span>}
                      </div>
                      {scrape.candidate_decision.ranked_candidates.length > 0 && (
                        <ul className="media-v4-work-detail-candidates">
                          {scrape.candidate_decision.ranked_candidates.slice(0, 5).map((candidate) => (
                            <li key={`${candidate.provider}:${candidate.provider_id}`}>
                              <span>{candidate.title || candidate.original_title || '未命名候选'}</span>
                              {candidate.score != null && <span>匹配度 {Math.round(candidate.score)}</span>}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
              )}
              {orderedSeasons.length > 0 && (
                <div className="media-v4-work-detail-section">
                  <h4>季度结构</h4>
                  <div className="media-v4-work-detail-seasons">
                    {orderedSeasons.map((season, index) => (
                      <span key={`${season.season_kind}:${season.season_number}:${index}`}>
                        {seasonLabel(season)} · {season.episode_count} 集
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <div className="media-v4-work-detail-section">
                <h4>镜像结果</h4>
                <div className="media-v4-work-detail-facts">
                  <span>{mirrorStatusLabel[detail.detail.mirror.status] ?? detail.detail.mirror.status}</span>
                  <span>{detail.detail.mirror.artifact_count} 个播放文件</span>
                </div>
                {detail.detail.mirror.error && <div className="media-v4-job-error" role="alert">{detail.detail.mirror.error}</div>}
                {detail.detail.mirror.artifacts.length > 0 && (
                  <ul className="media-v4-work-detail-artifacts">
                    {detail.detail.mirror.artifacts.map((artifact) => (
                      <li key={artifact.file_name}>{artifact.file_name}</li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="media-v4-work-detail-section">
                <h4>剧集结果{detail.detail.episode_total > 0 ? `（${detail.detail.episode_total} 集）` : ''}</h4>
                {detailEpisodes.length === 0 ? (
                  <div className="media-v4-work-detail-empty">本次任务未生成剧集结果。</div>
                ) : (
                  <div className="media-v4-work-detail-episodes">
                    {detailEpisodes.map((episode) => (
                      <div className="media-v4-work-detail-episode" key={episode.episode_id}>
                        <span className="media-v4-work-detail-episode-code">
                          {episode.season_kind === 'special' ? `SP${String(episode.episode_number ?? 0).padStart(2, '0')}` : `S${String(episode.season_number).padStart(2, '0')}E${String(episode.episode_number ?? 0).padStart(2, '0')}`}
                        </span>
                        <div className="media-v4-work-detail-episode-content">
                          <strong className="media-v4-work-detail-episode-name">{episode.scraped_title || episode.display_title || '未命名'}</strong>
                          <div className="media-v4-work-detail-episode-meta">
                            {episode.display_title && episode.scraped_title && episode.display_title !== episode.scraped_title && <span>本地名称：{episode.display_title}</span>}
                            {episode.file_name && <span>{episode.file_name}</span>}
                            {episode.provider_episode_id && <span>{detail.detail.work.provider === 'tmdb' ? 'TMDB' : '在线'} 集号 {episode.provider_episode_id}</span>}
                            {detailRuntimeLabel(episode.runtime) && <span>{detailRuntimeLabel(episode.runtime)}</span>}
                          </div>
                          {episode.scraped_plot && <p className="media-v4-work-detail-episode-plot">{episode.scraped_plot}</p>}
                        </div>
                        <span className={`media-v4-work-detail-episode-state ${episode.mapped ? 'mapped' : 'unmapped'}`}>{episode.mapped ? '已映射' : '未映射'}</span>
                        {episode.playback_ready && <span className="media-v4-work-detail-episode-playable">可播放</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </article>
  )
}

export function V4ExecutionProgress({ progress, busyRetryId, onRetry, resolvingWorkId, onResolveMetadata, fetchWorkDetail }: V4ExecutionProgressProps) {
  const [completedOpen, setCompletedOpen] = useState(false)
  // 3.1：作品执行详情缓存。键 = workId + 两个任务状态；任务重新执行后
  // 状态变化使旧键失效，下一次展开重新读取。
  const [detailStates, setDetailStates] = useState<Map<string, WorkDetailState>>(new Map())
  const requestedDetailsRef = useRef(new Set<string>())
  const getWorkDetail = (workId: string, cacheKey: string): WorkDetailState | undefined => {
    const cacheKeyFull = `${progress.revision_id}:${workId}:${cacheKey}`
    return detailStates.get(cacheKeyFull) ?? (requestedDetailsRef.current.has(cacheKeyFull) ? { status: 'loading' } : undefined)
  }
  const requestWorkDetail = (workId: string, cacheKey: string) => {
    const cacheKeyFull = `${progress.revision_id}:${workId}:${cacheKey}`
    if (!fetchWorkDetail || requestedDetailsRef.current.has(cacheKeyFull)) return
    requestedDetailsRef.current.add(cacheKeyFull)
    setDetailStates((current) => {
      const next = new Map(current)
      next.set(cacheKeyFull, { status: 'loading' })
      return next
    })
    fetchWorkDetail(progress.revision_id, workId)
      .then((detail) => {
        setDetailStates((current) => {
          const next = new Map(current)
          next.set(cacheKeyFull, { status: 'loaded', detail })
          return next
        })
      })
      .catch((cause: unknown) => {
        setDetailStates((current) => {
          const next = new Map(current)
          next.set(cacheKeyFull, { status: 'error', message: cause instanceof Error ? cause.message : String(cause) })
          return next
        })
      })
  }
  const sorted = sortWorkUnits(progress.work_units)
  const active = sorted.filter((unit) => unit.overall_status !== 'completed')
  const completed = sorted.filter((unit) => unit.overall_status === 'completed')
  const visibleCompleted = completedOpen ? completed : completed.slice(0, COMPLETED_PREVIEW_COUNT)
  const mirrorTotal = progress.stage_summary.mirror.total
  const mirrorDone = progress.stage_summary.mirror.succeeded + progress.stage_summary.mirror.failed + progress.stage_summary.mirror.cancelled
  const projectionFailed = progress.stage_summary.projection.status === 'failed' || progress.stage_summary.projection.status === 'cancelled'
  const projectionJob = projectionFailed ? progress.work_units[0] : null
  void projectionJob

  const renderWorkUnit = (unit: V4WorkProgressUnit) => (
    <WorkUnit
      key={unit.work_id}
      unit={unit}
      getWorkDetail={getWorkDetail}
      requestWorkDetail={requestWorkDetail}
      busyRetryId={busyRetryId}
      onRetry={onRetry}
      resolvingWorkId={resolvingWorkId}
      onResolveMetadata={onResolveMetadata}
    />
  )

  return (
    <div className="media-v4-execution-progress">
      <div className="media-v4-execution-head">
        <strong className="media-v4-execution-status">{EXECUTION_STATUS_LABELS[progress.overall_status]}</strong>
      </div>
      <ProgressBar value={mirrorTotal > 0 ? mirrorDone / mirrorTotal : 0} max={1} aria-label="总体进度" />
      <div className="media-v4-stage-pills" role="group" aria-label="用户阶段">
        {STAGE_KEYS.map((stageKey) => <StageSummary key={stageKey} stageKey={stageKey} progress={progress} />)}
      </div>
      {projectionFailed && (
        <div className="media-v4-projection-error" role="alert">
          <ErrorCircle24Regular aria-hidden="true" />
          <span>媒体库更新阶段未能完成，请检查镜像与媒体信息任务后重试。</span>
        </div>
      )}
      <div className="media-v4-work-progress-list" aria-label="作品进度">
        {active.map((unit) => renderWorkUnit(unit))}
      </div>
      {completed.length > 0 && (
        <div className="media-v4-completed-block">
          <div className="media-v4-completed-heading"><strong>已完成 {completed.length} 部</strong></div>
          <div className="media-v4-work-progress-list">
            {visibleCompleted.map((unit) => renderWorkUnit(unit))}
          </div>
          {completed.length > COMPLETED_PREVIEW_COUNT && (
            <button type="button" className="media-v4-completed-toggle" aria-expanded={completedOpen} onClick={() => setCompletedOpen((value) => !value)}>
              {completedOpen ? <ChevronDown24Regular aria-hidden="true" /> : <ChevronRight24Regular aria-hidden="true" />}
              <span>{completedOpen ? '收起已完成作品' : `显示其余 ${completed.length - COMPLETED_PREVIEW_COUNT} 部`}</span>
            </button>
          )}
        </div>
      )}
      {progress.work_units.length === 0 && <div className="media-v4-empty">这个来源没有作品任务。</div>}
    </div>
  )
}
