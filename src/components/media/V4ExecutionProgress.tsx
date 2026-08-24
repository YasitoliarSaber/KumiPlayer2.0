/**
 * P-003 第三步：V4 执行进度 = 总体阶段 + 作品单元。
 *
 * 消费后端 execution progress DTO（V4ExecutionProgress）。每部作品一个进度框，
 * 状态由 mirror + metadata jobs 组合推导；完成项默认折叠。原始 job_id 只作为
 * 重试命令参数，普通界面不展示 UUID 文本。
 */

import { useState } from 'react'
import { Button, ProgressBar } from '@fluentui/react-components'
import { CheckmarkCircle24Filled, ChevronDown24Regular, ChevronRight24Regular, DismissCircle24Regular, ErrorCircle24Regular, SpinnerIosRegular } from '@fluentui/react-icons/fonts'
import type { V4ExecutionProgress, V4WorkProgressUnit } from '../../api/mediaV4'
import { STAGE_LABELS, WORK_PROGRESS_LABELS, sortWorkUnits } from '../../lib/mediaSummary'

export interface V4ExecutionProgressProps {
  progress: V4ExecutionProgress
  busyRetryId: string
  onRetry: (jobId: string) => void
}

const STAGE_KEYS = ['mirror', 'metadata', 'projection'] as const

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

function WorkUnit({ unit, busyRetryId, onRetry }: { unit: V4WorkProgressUnit; busyRetryId: string; onRetry: (jobId: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const failedJob = unit.overall_status === 'failed'
    ? (unit.mirror.status === 'failed' ? unit.mirror : unit.metadata.status === 'failed' ? unit.metadata : null)
    : null
  return (
    <article className={`media-v4-work-progress media-v4-work-progress-${unit.overall_status}`}>
      <button type="button" className="media-v4-work-progress-head" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <span className="media-v4-work-progress-state" aria-hidden="true">
          {unit.overall_status === 'completed' ? <CheckmarkCircle24Filled /> : unit.overall_status === 'failed' ? <ErrorCircle24Regular /> : unit.overall_status === 'cancelled' ? <DismissCircle24Regular /> : <SpinnerIosRegular />}
        </span>
        <span className="media-v4-work-progress-title">{unit.title}</span>
        <span className="media-v4-work-progress-meta">
          {unit.media_type === 'movie' ? '电影' : '剧集'} · {unit.asset_count} 个文件{unit.episode_count > 0 ? ` · ${unit.episode_count} 集` : ''}
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
          {unit.overall_status === 'needs_attention' && <span className="media-v4-anomaly-tag">需要人工检查媒体信息</span>}
          {!failedJob && unit.overall_status !== 'completed' && <span className="media-v4-work-progress-hint">任务进行中，完成后自动折叠到“已完成”。</span>}
        </div>
      )}
    </article>
  )
}

export function V4ExecutionProgress({ progress, busyRetryId, onRetry }: V4ExecutionProgressProps) {
  const [completedOpen, setCompletedOpen] = useState(false)
  const sorted = sortWorkUnits(progress.work_units)
  const active = sorted.filter((unit) => unit.overall_status !== 'completed')
  const completed = sorted.filter((unit) => unit.overall_status === 'completed')
  const mirrorTotal = progress.stage_summary.mirror.total
  const mirrorDone = progress.stage_summary.mirror.succeeded + progress.stage_summary.mirror.failed + progress.stage_summary.mirror.cancelled
  const percent = mirrorTotal > 0 ? Math.round((mirrorDone / mirrorTotal) * 100) : 0
  const projectionFailed = progress.stage_summary.projection.status === 'failed' || progress.stage_summary.projection.status === 'cancelled'
  const projectionJob = projectionFailed ? progress.work_units[0] : null
  void projectionJob

  return (
    <div className="media-v4-execution-progress">
      <div className="media-v4-execution-head">
        <div>
          <strong>{progress.overall_status === 'completed' ? '媒体库已建立' : progress.overall_status === 'running' ? '正在建立媒体库' : progress.overall_status === 'needs_attention' ? '有任务需要处理' : '正在准备任务'}</strong>
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
        {active.map((unit) => <WorkUnit key={unit.work_id} unit={unit} busyRetryId={busyRetryId} onRetry={onRetry} />)}
      </div>
      {completed.length > 0 && (
        <div className="media-v4-completed-block">
          <button type="button" className="media-v4-completed-toggle" aria-expanded={completedOpen} onClick={() => setCompletedOpen((value) => !value)}>
            {completedOpen ? <ChevronDown24Regular aria-hidden="true" /> : <ChevronRight24Regular aria-hidden="true" />}
            <span>已完成 {completed.length} 部</span>
          </button>
          {completedOpen && (
            <div className="media-v4-work-progress-list">
              {completed.map((unit) => <WorkUnit key={unit.work_id} unit={unit} busyRetryId={busyRetryId} onRetry={onRetry} />)}
            </div>
          )}
        </div>
      )}
      {progress.work_units.length === 0 && <div className="media-v4-empty">这个来源没有作品任务。</div>}
    </div>
  )
}
