/**
 * P-003 第二步：V4 识别结果的可审核紧凑摘要。
 *
 * 只消费 V4Preview；默认按作品卡展示，季度/电影/特别篇为次级层级，默认折叠；
 * 存在 issue 或集号跨度异常的作品置顶并展开。人工修正通过带原始证据上下文的
 * Dialog 完成，仍调用 V4 override API；本组件不自行改写任何身份。
 */

import { useState } from 'react'
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, FluentProvider, Input, Select } from '@fluentui/react-components'
import { ChevronDown24Regular, ChevronRight24Regular, Dismiss24Regular, Warning24Regular } from '@fluentui/react-icons'
import type { V4Preview, V4ReviewIssue, V4SourceEvidence } from '../../api/mediaV4'
import { buildWorkSummaries, type RecognitionSummary, type WorkSummary } from '../../lib/mediaSummary'
import { getKumiFluentTheme } from '../../design/fluentTheme'
import { useUiStore } from '../../stores/ui'

const identityIssueCodes = new Set(['work_identity_conflict', 'historical_identity_conflict', 'provider_identity_conflict', 'work_identity_ambiguous', 'structural_identity_ambiguous'])

export interface OverrideDraft {
  title: string
  mediaType: 'tv' | 'movie'
  season: string
  episode: string
}

export interface V4RecognitionSummaryProps {
  preview: V4Preview
  issues: V4ReviewIssue[]
  evidenceEntries: V4SourceEvidence[]
  overrideDrafts: Record<string, OverrideDraft>
  busy: boolean
  onOverrideChange: (evidenceId: string, draft: OverrideDraft) => void
  onApplyOverride: (evidenceId: string) => void
  onOpenMaintenance?: () => void
}

function WorkCard({
  work,
  defaultExpanded,
  evidenceEntries,
  overrideDrafts,
  busy,
  onOverrideChange,
  onApplyOverride,
  onOpenMaintenance,
}: {
  work: WorkSummary
  defaultExpanded: boolean
  evidenceEntries: V4SourceEvidence[]
  overrideDrafts: Record<string, OverrideDraft>
  busy: boolean
  onOverrideChange: (evidenceId: string, draft: OverrideDraft) => void
  onApplyOverride: (evidenceId: string) => void
  onOpenMaintenance?: () => void
}) {
  const appearanceMode = useUiStore((state) => state.appearanceMode)
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [techOpen, setTechOpen] = useState(false)
  const [activeIssue, setActiveIssue] = useState<V4ReviewIssue | null>(null)
  const evidenceFor = (evidenceId: string) => evidenceEntries.find((entry) => entry.evidence_id === evidenceId)
  const activeEvidence = activeIssue ? evidenceFor(activeIssue.evidence_id) : undefined
  const activeDraft = activeIssue ? overrideDrafts[activeIssue.evidence_id] : undefined
  const identityIssue = activeIssue !== null && identityIssueCodes.has(activeIssue.code)
  return (
    <article className={`media-v4-work-card ${work.hasAnomaly ? 'attention' : ''}`}>
      <button type="button" className="media-v4-work-card-head" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <span className="media-v4-work-card-title">{work.title}</span>
        <span className="media-v4-work-card-meta">
          {work.year || '年份未知'} · {work.media_type === 'movie' ? '电影' : '剧集'} · 共 {work.assetCount} 个视频
          {work.hasAnomaly && <Warning24Regular aria-label="存在需处理问题" />}
        </span>
        {expanded ? <ChevronDown24Regular aria-hidden="true" /> : <ChevronRight24Regular aria-hidden="true" />}
      </button>
      {expanded && (
        <div className="media-v4-work-card-body">
          <div className="media-v4-work-group-list">
            {work.groups.map((group, index) => (
              <div className={`media-v4-work-group ${group.spanAnomaly ? 'attention' : ''}`} key={`${group.kind}-${group.seasonNumber ?? index}`}>
                <span>
                  {group.kind === 'movie' ? '电影' : group.kind === 'special' ? '特别篇' : `第 ${group.seasonNumber} 季`}
                  {' · '}
                  {group.episodeCount} 集 · {group.fileCount} 个文件
                </span>
                {group.rangeLabel && <code>{group.rangeLabel}</code>}
                {group.spanAnomaly && <span className="media-v4-anomaly-tag">集号跨度异常</span>}
              </div>
            ))}
          </div>
          {work.issues.length > 0 && (
            <div className="media-v4-work-issues-inline">
              {work.issues.map((issue) => (
                <div key={`${issue.code}-${issue.evidence_id}`} className="media-v4-issue-row">
                  <strong>{identityIssueCodes.has(issue.code) ? '作品身份冲突' : '识别需要修正'}</strong>
                  <span>{issue.message}</span>
                  <Button appearance="secondary" size="small" disabled={busy} onClick={() => setActiveIssue(issue)}>{identityIssueCodes.has(issue.code) ? '处理身份冲突' : '修正'}</Button>
                </div>
              ))}
            </div>
          )}
          {work.episodeCount > 20 && (
            <button type="button" className="media-v4-tech-details-toggle" aria-expanded={techOpen} onClick={() => setTechOpen((value) => !value)}>
              {techOpen ? '收起' : '查看'}技术详情（{work.episodeCount} 集）
            </button>
          )}
          {techOpen && (
            <div className="media-v4-tech-details">
              {work.issues.slice(0, 50).map((issue) => (
                <code key={`${issue.code}-${issue.evidence_id}`}>{issue.evidence_id} · {issue.message}</code>
              ))}
            </div>
          )}
        </div>
      )}
      {activeIssue && (
        <Dialog open onOpenChange={(_, data) => { if (!data.open) setActiveIssue(null) }}>
          <DialogSurface className="media-v4-recognition-dialog" backdrop={{ className: 'media-v4-recognition-backdrop' }} aria-describedby={undefined}>
            <FluentProvider theme={getKumiFluentTheme(appearanceMode)}>
            <DialogBody>
              <DialogTitle action={<Button appearance="subtle" aria-label="关闭" icon={<Dismiss24Regular />} onClick={() => setActiveIssue(null)} />}>
                {identityIssue ? '处理作品身份冲突' : '修正识别结果'}
              </DialogTitle>
              <DialogContent>
                {identityIssue ? (
                  <div className="media-v4-identity-help">
                    <p>{activeIssue.message}</p>
                    <p>修改单集标题或集号不能解决作品身份冲突。历史自动识别不代表绑定正确；已清理来源的旧绑定不会再用于新导入。</p>
                    <p>如果错误作品仍在媒体库中，可打开媒体库维护，选择对应来源、核对清理预览后再重新导入。此处不会直接删除任何内容；清理会影响该来源的媒体记录与受控生成物，请先检查范围。</p>
                  </div>
                ) : (<>
                {activeEvidence && (
                  <div className="media-v4-issue-evidence" role="note">
                    <div><strong>原始文件</strong><code title={activeEvidence.source_key}>{activeEvidence.relative_path || activeEvidence.source_key}</code></div>
                    <div><strong>解析原因</strong><span>{activeIssue.message}</span></div>
                  </div>
                )}
                <div className="media-v4-override-row media-v4-override-dialog-row">
                  <Field label="作品标题">
                  <Input
                    aria-label="修正作品标题"
                    value={activeDraft?.title ?? ''}
                    placeholder="例如：偶像大师 灰姑娘女孩 U149"
                    onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...(activeDraft ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), title: data.value })}
                  />
                  </Field>
                  <Field label="媒体类型">
                  <Select
                    aria-label="修正媒体类型"
                    value={activeDraft?.mediaType ?? 'tv'}
                    onChange={(event) => onOverrideChange(activeIssue.evidence_id, { ...(activeDraft ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), mediaType: event.currentTarget.value as 'tv' | 'movie' })}
                  >
                    <option value="tv">剧集</option>
                    <option value="movie">电影</option>
                  </Select>
                  </Field>
                  {(activeDraft?.mediaType ?? 'tv') === 'tv' && (
                    <>
                      <Field label="季度">
                      <Input aria-label="修正季度" value={activeDraft?.season ?? '1'} placeholder="季度" onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...(activeDraft ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), season: data.value })} />
                      </Field>
                      <Field label="集号">
                      <Input aria-label="修正集号" value={activeDraft?.episode ?? '1'} placeholder="集号" onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...(activeDraft ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), episode: data.value })} />
                      </Field>
                    </>
                  )}
                </div>
                </>)}
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" disabled={busy} onClick={() => setActiveIssue(null)}>取消</Button>
                {identityIssue ? (
                  <Button appearance="primary" disabled={busy || !onOpenMaintenance} onClick={() => { setActiveIssue(null); onOpenMaintenance?.() }}>打开媒体库维护</Button>
                ) : (
                  <Button appearance="primary" disabled={busy || !activeDraft?.title.trim()} onClick={() => { onApplyOverride(activeIssue.evidence_id); setActiveIssue(null) }}>应用修正</Button>
                )}
              </DialogActions>
            </DialogBody>
            </FluentProvider>
          </DialogSurface>
        </Dialog>
      )}
    </article>
  )
}

export function V4RecognitionSummary({ preview, evidenceEntries, overrideDrafts, busy, onOverrideChange, onApplyOverride, onOpenMaintenance }: V4RecognitionSummaryProps) {
  const summary = buildWorkSummaries(preview)
  const cardProps = {
    evidenceEntries,
    overrideDrafts,
    busy,
    onOverrideChange,
    onApplyOverride,
    onOpenMaintenance,
  }
  return (
    <div className="media-v4-recognition-summary">
      <div className="media-v4-summary-numbers">
        <div><strong>{summary.totalWorks}</strong><span>部作品</span></div>
        <div><strong>{summary.totalFiles}</strong><span>个视频</span></div>
        <div><strong>{summary.totalVideos}</strong><span>条剧集记录</span></div>
        <div className={summary.totalIssues > 0 ? 'attention' : ''}><strong>{summary.totalIssues}</strong><span>项需处理问题</span></div>
      </div>
      {summary.totalIssues > 0 && (
        <div className="media-v4-issues-block">
          <h3>需要处理</h3>
          <div className="media-v4-work-grid">
            {summary.anomalyWorks.map((work) => (
              <WorkCard key={work.work_key} work={work} defaultExpanded {...cardProps} />
            ))}
          </div>
        </div>
      )}
      <div className="media-v4-work-grid">
        {summary.normalWorks.map((work) => (
          <WorkCard key={work.work_key} work={work} defaultExpanded={false} {...cardProps} />
        ))}
      </div>
      {summary.totalWorks === 0 && <div className="media-v4-empty">这个来源没有可建立媒体库的作品。</div>}
    </div>
  )
}

export type { RecognitionSummary }
