/**
 * P-005：V4 媒体库维护面板。
 *
 * 只消费 maintenance API 的 preview/confirm DTO；预览分组明确“将删除 / 将保留 /
 * 已阻止 / 始终保留”，最终确认使用危险色且 blocked/过期/未生成时禁用。
 * 来源选项只包含 local / pan115 / baidu / quark / all，不出现 OpenList。
 */

import { useState } from 'react'
import { Button, MessageBar, MessageBarBody, Radio, RadioGroup, Spinner } from '@fluentui/react-components'
import { Delete24Regular, ShieldCheckmark24Regular } from '@fluentui/react-icons/fonts'
import type { V4MaintenancePreview, V4MaintenanceResult } from '../../api/mediaV4'

export interface LibraryMaintenancePanelProps {
  busy: boolean
  onPreview: (scope: string) => Promise<V4MaintenancePreview>
  onConfirm: (preview: V4MaintenancePreview) => Promise<V4MaintenanceResult>
}

const SCOPES = [
  { value: 'all', label: '全部来源' },
  { value: 'local', label: '本地' },
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'quark', label: '夸克网盘' },
]

export function LibraryMaintenancePanel({ busy, onPreview, onConfirm }: LibraryMaintenancePanelProps) {
  const [scope, setScope] = useState('all')
  const [preview, setPreview] = useState<V4MaintenancePreview | null>(null)
  const [result, setResult] = useState<V4MaintenanceResult | null>(null)
  const [error, setError] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const generatePreview = async () => {
    setError('')
    setResult(null)
    setPreviewing(true)
    try {
      const next = await onPreview(scope)
      setPreview(next)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '生成删除预览失败')
    } finally {
      setPreviewing(false)
    }
  }

  const confirmDelete = async () => {
    if (!preview) return
    setError('')
    setConfirming(true)
    try {
      const next = await onConfirm(preview)
      setResult(next)
      setPreview(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败')
    } finally {
      setConfirming(false)
    }
  }

  return (
    <section className="media-v4-maintenance" aria-label="媒体库维护">
      <div className="media-stage-heading">
        <span className="media-stage-icon" aria-hidden="true"><ShieldCheckmark24Regular /></span>
        <div><span className="media-stage-eyebrow">媒体库维护</span><h2>按来源清理</h2><p>删除 KumiPlayer 媒体库数据与受控生成物；源视频、外部 TXT、网盘对象、设置与凭据始终保留。</p></div>
      </div>

      <div className="media-v4-maintenance-scope">
        <RadioGroup value={scope} onChange={(_, data) => { setScope(data.value); setPreview(null); setResult(null); setError('') }} aria-label="清理来源范围">
          {SCOPES.map((option) => <Radio key={option.value} value={option.value} label={option.label} />)}
        </RadioGroup>
      </div>

      <div className="media-v4-command-row media-v4-maintenance-actions">
        <div><strong>生成删除预览</strong><span>预览只计算影响范围，不会删除任何内容。</span></div>
        <Button appearance="secondary" icon={<Delete24Regular />} disabled={busy || previewing} onClick={() => void generatePreview()}>
          {previewing ? <Spinner size="tiny" /> : '生成删除预览'}
        </Button>
      </div>

      {error && <MessageBar intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      {preview && (
        <div className="media-v4-maintenance-preview">
          {preview.blocked && (
            <MessageBar intent="warning"><MessageBarBody>该来源仍有正在运行的后台任务，清理已阻止；请等待任务完成后再生成预览。</MessageBarBody></MessageBar>
          )}
          <div className="media-v4-summary-numbers">
            <div><strong>{preview.root_count}</strong><span>个来源根</span></div>
            <div><strong>{preview.work_count}</strong><span>部作品</span></div>
            <div className="attention"><strong>{preview.orphan_work_count}</strong><span>部将退出媒体库</span></div>
            <div><strong>{preview.mixed_work_count}</strong><span>部混合来源将保留</span></div>
            <div><strong>{preview.asset_count}</strong><span>个媒体文件</span></div>
            <div><strong>{preview.artifact_count}</strong><span>项受控生成物</span></div>
          </div>
          <div className="media-v4-maintenance-groups">
            <div><strong>将删除</strong><span>{preview.orphan_work_count} 部作品的媒体库记录与受控镜像/NFO/图片</span></div>
            <div><strong>将保留</strong><span>{preview.mixed_work_count} 部混合来源作品完整保留（含个人状态）</span></div>
            <div><strong>始终保留</strong>
              <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </div>
          </div>
          <div className="media-v4-maintenance-confirm">
            <Button appearance="primary" icon={<Delete24Regular />} disabled={busy || confirming || preview.blocked} onClick={() => void confirmDelete()}>
              {confirming ? <><Spinner size="tiny" />正在清理</> : '确认清理此来源'}
            </Button>
            <span>确认后将标记来源退役并精确清理预览列出的受控生成物。</span>
          </div>
        </div>
      )}

      {result && (
        <div className="media-v4-maintenance-result" role="status">
          <MessageBar intent={result.projection_status === 'ok' ? 'success' : 'warning'}>
            <MessageBarBody>清理完成：退役 {result.retired_roots.length} 个来源根，{result.orphan_works.length} 部作品退出媒体库，{result.mixed_works.length} 部混合来源保留。</MessageBarBody>
          </MessageBar>
          <div className="media-v4-maintenance-result-detail">
            {result.artifact_results.slice(0, 20).map((item) => (
              <span key={item.path} className={`artifact-${item.status}`}>{item.status === 'removed' ? '已删除' : item.status === 'missing' ? '已不存在' : item.status === 'blocked' ? '已阻止' : '失败'} · {item.path}</span>
            ))}
            {result.artifact_results.length === 0 && <span>没有需要清理的受控生成物。</span>}
          </div>
        </div>
      )}
    </section>
  )
}
