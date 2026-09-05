/**
 * P-003 第三步：V4 执行进度 = 总体阶段 + 作品单元。
 *
 * 消费后端 execution progress DTO（V4ExecutionProgress）。每部作品一个进度框，
 * 状态由 mirror + metadata jobs 组合推导；完成项默认折叠。原始 job_id 只作为
 * 重试命令参数，普通界面不展示 UUID 文本。
 */

import { useEffect, useState } from 'react'
import { Button, ProgressBar } from '@fluentui/react-components'
import { CheckmarkCircle24Filled, ChevronDown24Regular, ChevronRight24Regular, DismissCircle24Regular, ErrorCircle24Regular, SpinnerIosRegular, Warning24Regular } from '@fluentui/react-icons'
import type { V4ExecutionProgress, V4WorkProgressUnit } from '../../api/mediaV4'
import { STAGE_LABELS, WORK_PROGRESS_LABELS, sortWorkUnits } from '../../lib/mediaSummary'

export interface V4ExecutionProgressProps {
  progress: V4ExecutionProgress
  busyRetryId: string
  onRetry: (jobId: string) => void
  resolvingWorkId: string
  onResolveMetadata: (workId: string) => void
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

function WorkUnit({ unit, busyRetryId, onRetry, resolvingWorkId, onResolveMetadata }: {
  unit: V4WorkProgressUnit
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
  return (
    <article className={`media-v4-work-progress media-v4-work-progress-${unit.overall_status}`}>
      <button type="button" className="media-v4-work-progress-head" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
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
        <div className="media-v4-work-progress-body">
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
        </div>
      )}
    </article>
  )
}

export function V4ExecutionProgress({ progress, busyRetryId, onRetry, resolvingWorkId, onResolveMetadata }: V4ExecutionProgressProps) {
  const [completedOpen, setCompletedOpen] = useState(false)
  const sorted = sortWorkUnits(progress.work_units)
  const active = sorted.filter((unit) => unit.overall_status !== 'completed')
  const completed = sorted.filter((unit) => unit.overall_status === 'completed')
  const visibleCompleted = completedOpen ? completed : completed.slice(0, COMPLETED_PREVIEW_COUNT)
  const mirrorTotal = progress.stage_summary.mirror.total
  const mirrorDone = progress.stage_summary.mirror.succeeded + progress.stage_summary.mirror.failed + progress.stage_summary.mirror.cancelled
  const projectionFailed = progress.stage_summary.projection.status === 'failed' || progress.stage_summary.projection.status === 'cancelled'
  const projectionJob = projectionFailed ? progress.work_units[0] : null
  void projectionJob

  return (
    <div className="media-v4-execution-progress">
      <div className="media-v4-execution-head">
        <div>
          <strong>{EXECUTION_STATUS_LABELS[progress.overall_status]}</strong>
          <span className="media-v4-execution-overall">{progress.work_units.length} 部作品 · {mirrorDone}/{mirrorTotal} 已完成镜像</span>
        </div>
        <ProgressBar value={mirrorTotal > 0 ? mirrorDone / mirrorTotal : 0} max={1} aria-label="总体进度" />
      </div>
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
        {active.map((unit) => <WorkUnit key={unit.work_id} unit={unit} busyRetryId={busyRetryId} onRetry={onRetry} resolvingWorkId={resolvingWorkId} onResolveMetadata={onResolveMetadata} />)}
      </div>
      {completed.length > 0 && (
        <div className="media-v4-completed-block">
          <div className="media-v4-completed-heading"><strong>已完成 {completed.length} 部</strong><span>可展开查看每部作品的执行结果</span></div>
          <div className="media-v4-work-progress-list">
            {visibleCompleted.map((unit) => <WorkUnit key={unit.work_id} unit={unit} busyRetryId={busyRetryId} onRetry={onRetry} resolvingWorkId={resolvingWorkId} onResolveMetadata={onResolveMetadata} />)}
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
