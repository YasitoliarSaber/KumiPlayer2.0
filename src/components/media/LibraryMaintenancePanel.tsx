/**
 * P-005：V4 媒体库维护面板。
 *
 * 只消费 maintenance API 的 preview/confirm DTO；预览分组明确“将删除 / 将保留 /
 * 已阻止 / 始终保留”，最终确认使用危险色且 blocked/过期/未生成时禁用。
 * 来源选项只包含 local / pan115 / baidu / quark / all，不出现 OpenList。
 */

import { useState } from 'react'
import {
  Button,
  MessageBar,
  MessageBarBody,
  Radio,
  RadioGroup,
  Spinner,
} from '@fluentui/react-components'
import { Delete24Regular, ShieldCheckmark24Regular } from '@fluentui/react-icons'
import type { V4MaintenancePreview, V4MaintenanceResult } from '../../api/mediaV4'

export interface LibraryMaintenancePanelProps {
  busy: boolean
  onPreview: (scope: string) => Promise<V4MaintenancePreview>
  onConfirm: (preview: V4MaintenancePreview) => Promise<V4MaintenanceResult>
  onResume: (previewId: string) => Promise<V4MaintenanceResult>
}

const SCOPES = [
  { value: 'all', label: '全部来源' },
  { value: 'local', label: '本地' },
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'quark', label: '夸克网盘' },
]

const PROVIDER_LABELS: Record<string, string> = {
  local: '本地',
  pan115: '115 网盘',
  baidu: '百度网盘',
  quark: '夸克网盘',
}

const JOB_TYPE_LABELS: Record<string, string> = {
  materialize_mirror: '生成镜像',
  scrape_work: '获取媒体信息',
  refresh_projection: '更新媒体库',
}

function sourceGroups(roots: V4MaintenancePreview['root_names']): string[] {
  const counts = new Map<string, number>()
  for (const root of roots) counts.set(root.provider, (counts.get(root.provider) ?? 0) + 1)
  return [...counts.entries()].map(([provider, count]) => `${PROVIDER_LABELS[provider] ?? provider} · ${count} 个来源`)
}

function skippedSourceSummary(preview: V4MaintenancePreview): string {
  const count = preview.skipped_root_count ?? 0
  if (count === 0) return ''
  const providers = (preview.skipped_provider_counts ?? [])
    .map((item) => `${PROVIDER_LABELS[item.provider] ?? item.provider} ${item.count} 个`)
    .join('、')
  return `${count} 个未完成导入的来源已跳过${providers ? `（${providers}）` : ''}；它们没有可清理的媒体库数据。`
}

function maintenanceError(cause: unknown, fallback: string): string {
  const message = cause instanceof Error ? cause.message : fallback
  if (/没有已确认 revision|来源根\s+root_/u.test(message)) {
    return '所选范围包含尚未完成导入的来源；请先完成导入，或重新生成删除预览。'
  }
  if (/删除预览已过期|重新生成删除预览|来源、revision、任务或生成物已变化/u.test(message)) {
    return '清理条件已变化，请重新生成删除预览。'
  }
  if (/正在运行的后台任务/u.test(message)) {
    return '当前仍有后台任务在运行，请等待完成后重新检查。'
  }
  return `${fallback}，请重新检查后重试`
}

function blockedJobSummary(preview: V4MaintenancePreview): string {
  return preview.blocked_job_types
    .map((jobType) => JOB_TYPE_LABELS[jobType] ?? '后台处理')
    .join('、')
}

export function LibraryMaintenancePanel({ busy, onPreview, onConfirm, onResume }: LibraryMaintenancePanelProps) {
  const [scope, setScope] = useState('all')
  const [preview, setPreview] = useState<V4MaintenancePreview | null>(null)
  const [result, setResult] = useState<V4MaintenanceResult | null>(null)
  const [error, setError] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [resuming, setResuming] = useState(false)

  const generatePreview = async () => {
    setError('')
    setResult(null)
    setPreviewing(true)
    try {
      const next = await onPreview(scope)
      setPreview(next)
    } catch (cause) {
      setError(maintenanceError(cause, '生成删除预览失败'))
    } finally {
      setPreviewing(false)
    }
  }

  const resumeDelete = async () => {
    if (!result) return
    setError('')
    setResuming(true)
    try {
      const next = await onResume(result.preview_id)
      setResult(next)
    } catch (cause) {
      setError(maintenanceError(cause, '恢复清理失败'))
    } finally {
      setResuming(false)
    }
  }

  const confirmDelete = async () => {
    if (!preview) return
    if (preview.blocked) {
      setError('当前仍有后台任务在运行，请重新检查清理条件后再继续。')
      return
    }
    setError('')
    setConfirming(true)
    try {
      const next = await onConfirm(preview)
      setResult(next)
      setPreview(null)
    } catch (cause) {
      setError(maintenanceError(cause, '删除失败'))
    } finally {
      setConfirming(false)
    }
  }

  return (
    <section className="media-v4-maintenance" aria-label="媒体库维护">
      <div className="media-stage-heading">
        <span className="media-stage-icon" aria-hidden="true"><ShieldCheckmark24Regular /></span>
        <div><span className="media-stage-eyebrow">媒体库维护</span><h2>按来源清理</h2><p>清理媒体库记录和受控生成物，不会删除源文件、外部清单、设置或凭据。</p></div>
      </div>

      <div className="media-v4-maintenance-scope">
        <div className="media-v4-maintenance-section-heading">
          <strong>清理范围</strong>
        </div>
        <RadioGroup value={scope} onChange={(_, data) => { setScope(data.value); setPreview(null); setResult(null); setError('') }} aria-label="清理来源范围">
          {SCOPES.map((option) => (
            <Radio
              key={option.value}
              className="media-v4-maintenance-scope-option"
              value={option.value}
              label={<span className="media-v4-maintenance-scope-copy"><strong>{option.label}</strong></span>}
            />
          ))}
        </RadioGroup>
      </div>

      <div className="media-v4-command-row media-v4-maintenance-actions">
        <div><strong>删除预览</strong><span>先查看影响范围，再执行清理。</span></div>
        <Button className="media-v4-maintenance-preview-button" appearance="outline" icon={<Delete24Regular />} disabled={busy || previewing} onClick={() => void generatePreview()}>
          {previewing ? <Spinner size="tiny" /> : '生成删除预览'}
        </Button>
      </div>

      {error && <MessageBar intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      {preview && (
        <div className="media-v4-maintenance-preview">
          <div className="media-v4-maintenance-preview-heading">
            <div><span className="media-stage-eyebrow">删除预览</span><strong>确认受影响范围</strong></div>
            <span>{preview.work_count} 部作品 · {preview.artifact_count} 项受控生成物</span>
          </div>
          {preview.blocked && (
            <MessageBar intent="warning"><MessageBarBody>还有 {preview.blocked_job_count} 个后台任务正在{blockedJobSummary(preview)}。任务完成前不能清理，请稍后重新检查。</MessageBarBody></MessageBar>
          )}
          {skippedSourceSummary(preview) && (
            <MessageBar intent="info"><MessageBarBody>{skippedSourceSummary(preview)}</MessageBarBody></MessageBar>
          )}
          {preview.root_names.length > 0 && (
            <div className="media-v4-maintenance-root-names">
              <span>将退役来源</span>
              <div>{sourceGroups(preview.root_names).map((group) => <em key={group}>{group}</em>)}</div>
            </div>
          )}
          <div className="media-v4-maintenance-impact" aria-label="清理影响摘要">
            <div><strong>{preview.root_count}</strong><span>个来源根</span></div>
            <div className="attention"><strong>{preview.orphan_work_count}</strong><span>部将退出媒体库</span></div>
            <div><strong>{preview.mixed_work_count}</strong><span>部混合来源将保留</span></div>
          </div>
          <div className="media-v4-maintenance-groups">
            <div className="media-v4-maintenance-delete-group">
              <strong>将退出媒体库</strong>
              <span>{preview.orphan_work_count} 部作品的媒体库记录、受控镜像、NFO 和图片</span>
              <small>同时移除播放历史 {preview.history_count ?? 0} 条、播放进度 {preview.progress_count ?? 0} 条、追更状态 {preview.tracking_count ?? 0} 条。</small>
            </div>
            <div className="media-v4-maintenance-keep-group">
              <strong>保留内容</strong>
              <span>{preview.mixed_work_count} 部混合来源作品及其个人状态完整保留。</span>
              {preview.warnings.length > 0 && <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
            </div>
          </div>
          <div className="media-v4-maintenance-confirm">
            <div className={`media-v4-maintenance-confirm-status${preview.blocked ? ' blocked' : ''}`}>
              <strong>{preview.blocked ? '清理暂不可用' : '可以开始清理'}</strong>
              <span>{preview.blocked ? '后台任务完成后重新检查，确认按钮会恢复。' : '确认后立即清理上述媒体库数据。'}</span>
            </div>
            <div className="media-v4-maintenance-confirm-actions">
              {preview.blocked && (
                <Button appearance="outline" disabled={busy || previewing} onClick={() => void generatePreview()} aria-label="重新检查清理条件">
                  {previewing ? <Spinner size="tiny" /> : '重新检查'}
                </Button>
              )}
              <Button className="media-v4-maintenance-danger-button" appearance="primary" icon={<Delete24Regular />} disabled={busy || confirming || preview.blocked} onClick={() => void confirmDelete()}>
                {confirming ? <><Spinner size="tiny" />正在清理</> : preview.blocked ? '暂不可清理' : '确认清理'}
              </Button>
            </div>
          </div>
        </div>
      )}

      {result && (
        <div className="media-v4-maintenance-result" role="status">
          <MessageBar intent={result.status === 'completed' ? 'success' : 'warning'}>
            <MessageBarBody>
              {result.status === 'completed' ? `清理完成：退役 ${result.retired_root_count} 个来源根，${result.orphan_work_count} 部作品退出媒体库，${result.mixed_work_count} 部混合来源保留。`
                : result.status === 'partial_failed' ? '部分受控生成物清理失败，可重试未完成项；已退役来源不会重新激活。'
                  : result.status === 'projection_failed' ? '文件已清理，但媒体库投影重建失败，可重试投影。'
                    : `清理状态：${result.status}`}
            </MessageBarBody>
          </MessageBar>
          {(result.status === 'partial_failed' || result.status === 'projection_failed') && (
            <div className="media-v4-maintenance-retry">
              <Button appearance="primary" disabled={busy || resuming} onClick={() => void resumeDelete()}>
                {resuming ? <><Spinner size="tiny" />正在恢复</> : '重试未完成清理'}
              </Button>
            </div>
          )}
          <div className="media-v4-maintenance-result-detail">
            {result.artifact_results.map((item) => (
              <span key={item.path} className={`artifact-${item.status}`}>{item.status === 'removed' ? '已删除' : item.status === 'missing' ? '已不存在' : item.status === 'blocked' ? '已阻止' : '失败'} · {item.path}</span>
            ))}
            {result.artifact_results.length === 0 && <span>没有需要清理的受控生成物（{result.artifact_count} 项已全部完成或不存在）。</span>}
          </div>
        </div>
      )}
    </section>
  )
}
