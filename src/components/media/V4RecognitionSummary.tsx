/**
 * P-003 第二步：V4 识别结果的可审核紧凑摘要。
 *
 * 只消费 V4Preview；默认按作品卡展示，季度/电影/特别篇为次级层级，默认折叠；
 * 存在 issue 或集号跨度异常的作品置顶，按作品进入修正窗口。人工修正通过带原始证据上下文的
 * Dialog 完成，仍调用 V4 override API；本组件不自行改写任何身份。
 */

import { useState } from 'react'
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, FluentProvider, Input, Select } from '@fluentui/react-components'
import { ChevronDown24Regular, ChevronRight24Regular, Dismiss24Regular, Warning24Regular } from '@fluentui/react-icons'
import type { V4Preview, V4ReviewIssue, V4SourceEvidence } from '../../api/mediaV4'
import { buildWorkSummaries, type RecognitionSummary, type WorkSummary } from '../../lib/mediaSummary'
import { getKumiFluentTheme } from '../../design/fluentTheme'
import { useUiStore } from '../../stores/ui'
import './V4RecognitionSummary.css'

const identityIssueCodes = new Set(['work_identity_conflict', 'historical_identity_conflict', 'provider_identity_conflict', 'work_identity_ambiguous', 'structural_identity_ambiguous'])

export interface OverrideDraft {
  title: string
  mediaType: 'tv' | 'movie' | ''
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
  onApplyOverride: (evidenceId: string, draft: OverrideDraft) => Promise<boolean> | boolean
  onOpenMaintenance?: () => void
  onReimport?: () => void
}

function WorkCard({
  work,
  preview,
  defaultExpanded,
  evidenceEntries,
  overrideDrafts,
  busy,
  onOverrideChange,
  onApplyOverride,
  onOpenMaintenance,
  onReimport,
}: {
  work: WorkSummary
  preview: V4Preview
  defaultExpanded: boolean
  evidenceEntries: V4SourceEvidence[]
  overrideDrafts: Record<string, OverrideDraft>
  busy: boolean
  onOverrideChange: (evidenceId: string, draft: OverrideDraft) => void
  onApplyOverride: (evidenceId: string, draft: OverrideDraft) => Promise<boolean> | boolean
  onOpenMaintenance?: () => void
  onReimport?: () => void
}) {
  const appearanceMode = useUiStore((state) => state.appearanceMode)
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [techOpen, setTechOpen] = useState(false)
  const [activeIssue, setActiveIssue] = useState<V4ReviewIssue | null>(null)
  const evidenceFor = (evidenceId: string) => evidenceEntries.find((entry) => entry.evidence_id === evidenceId)
  const activeEvidence = activeIssue ? evidenceFor(activeIssue.evidence_id) : undefined
  const resolvedEpisode = activeIssue
    ? preview.episodes.find((episode) => episode.asset_evidence_ids.includes(activeIssue.evidence_id))
    : undefined
  const initialDraft: OverrideDraft = {
    title: work.title === '未命名' ? '' : work.title,
    mediaType: work.media_type === 'unknown' ? '' : work.media_type,
    season: resolvedEpisode?.local_season_number?.toString() ?? '',
    episode: resolvedEpisode?.local_episode_number?.toString() ?? '',
  }
  const activeDraft = activeIssue ? (overrideDrafts[activeIssue.evidence_id] ?? initialDraft) : undefined
  const hasValidTvNumbers = activeDraft?.mediaType !== 'tv' || (
    /^[1-9]\d*$/.test(activeDraft.season) && /^[1-9]\d*$/.test(activeDraft.episode)
  )
  const identityIssue = activeIssue !== null && identityIssueCodes.has(activeIssue.code)
  const typeLabel = work.media_type === 'movie' ? '电影' : work.media_type === 'unknown' ? '类型未定' : '剧集'
  // 未知媒体类型不是 TV 剧集：计数单位写成「个条目」，不冒充集数。
  const countUnit = work.media_type === 'unknown' ? '个条目' : '集'
  return (
    <article className={`media-v4-work-card ${work.hasAnomaly ? 'attention' : ''}`}>
      <button type="button" className="media-v4-work-card-head" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <span className="media-v4-work-card-title">{work.title}</span>
        <span className="media-v4-work-card-meta">
          {work.year || '年份未知'} · {typeLabel} · 共 {work.assetCount} 个视频
          {work.hasAnomaly && <Warning24Regular aria-label="存在需处理问题" />}
        </span>
        {expanded ? <ChevronDown24Regular aria-hidden="true" /> : <ChevronRight24Regular aria-hidden="true" />}
      </button>
      {work.issues.length > 0 && (
        <div className="media-v4-work-review-action">
          <span>{work.issues.length} 项待核对</span>
          <Button appearance="secondary" size="small" disabled={busy} onClick={() => setActiveIssue(work.issues[0])}>
            {preview.status === 'confirmed' ? '查看处理方式' : identityIssueCodes.has(work.issues[0].code) ? '处理身份冲突' : '修正'}
          </Button>
        </div>
      )}
      {expanded && (
        <div className="media-v4-work-card-body">
          <div className="media-v4-work-group-list">
            {work.groups.map((group, index) => (
              <div className={`media-v4-work-group ${group.spanAnomaly ? 'attention' : ''}`} key={`${group.kind}-${group.seasonNumber ?? (group.seasonUnknown ? 'unknown' : index)}`}>
                <span>
                  {group.kind === 'movie'
                    ? '电影'
                    : group.kind === 'special'
                      ? '特别篇'
                      : group.seasonUnknown
                        ? '季号未定'
                        : `第 ${group.seasonNumber} 季`}
                  {' · '}
                  {group.episodeCount} {countUnit} · {group.fileCount} 个文件
                </span>
                {group.rangeLabel && <code>{group.rangeLabel}</code>}
                {group.unresolvedEpisodeCount > 0 && group.hasNumberedItems && (
                  <span className="media-v4-unresolved-tag">另有 {group.unresolvedEpisodeCount} 项集号未定</span>
                )}
                {group.spanAnomaly && <span className="media-v4-anomaly-tag">集号跨度异常</span>}
              </div>
            ))}
          </div>
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
                {preview.status === 'confirmed' ? '更新识别结果' : identityIssue ? '处理作品身份冲突' : '修正识别结果'}
              </DialogTitle>
              <DialogContent>
                <p className="media-v4-review-work-title">{work.title}</p>
                {work.issues.length > 1 && (
                  <div className="media-v4-review-file-list" role="group" aria-label="选择待核对条目">
                    {work.issues.map((issue, index) => {
                      const evidence = evidenceFor(issue.evidence_id)
                      const filename = (evidence?.relative_path || evidence?.source_key || issue.evidence_id).split(/[\\/]/).pop()
                      return <Button key={`${issue.code}-${issue.evidence_id}`} appearance="secondary" disabled={busy} aria-pressed={activeIssue === issue} onClick={() => setActiveIssue(issue)}>{index + 1}. {filename}</Button>
                    })}
                  </div>
                )}
                {preview.status === 'confirmed' ? (
                  <div className="media-v4-identity-help">
                    <p>这次导入已建立媒体库，识别事实不能直接改写。重新扫描此来源后，可在确认前核对修正结果。</p>
                  </div>
                ) : identityIssue ? (
                  <div className="media-v4-identity-help">
                    <p>{activeIssue.message}</p>
                    <p>修改单集标题或集号不能解决作品身份冲突。历史自动识别不代表绑定正确；已清理来源的旧绑定不会再用于新导入。</p>
                    <p>如果错误作品仍在媒体库中，可打开媒体库维护，选择对应来源、核对清理预览后再重新导入。此处不会直接删除任何内容；清理会影响该来源的媒体记录与受控生成物，请先检查范围。</p>
                  </div>
                ) : (<>
                  <div className="media-v4-issue-evidence" role="note">
                    <strong>待修正文件</strong>
                    <code title={activeEvidence?.relative_path || activeEvidence?.source_key}>{(activeEvidence?.relative_path || activeEvidence?.source_key || activeIssue.evidence_id).split(/[\\/]/).pop()}</code>
                    <span>{activeIssue.message}</span>
                  </div>
                <div className="media-v4-override-row media-v4-override-dialog-row">
                  <Field label="作品标题">
                  <Input
                    aria-label="修正作品标题"
                    value={activeDraft?.title ?? ''}
                    placeholder="输入作品名称"
                    onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...initialDraft, ...activeDraft, title: data.value })}
                  />
                  </Field>
                  <Field label="媒体类型">
                  <Select
                    aria-label="修正媒体类型"
                    value={activeDraft?.mediaType ?? ''}
                    onChange={(event) => onOverrideChange(activeIssue.evidence_id, { ...initialDraft, ...activeDraft, mediaType: event.currentTarget.value as 'tv' | 'movie' })}
                  >
                    <option value="" disabled>请选择类型</option>
                    <option value="tv">剧集</option>
                    <option value="movie">电影</option>
                  </Select>
                  </Field>
                  {activeDraft?.mediaType === 'tv' && (
                    <>
                      <Field label="季度">
                      <Input aria-label="修正季度" inputMode="numeric" value={activeDraft?.season ?? ''} placeholder="未确定" onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...initialDraft, ...activeDraft, season: data.value })} />
                      </Field>
                      <Field label="集号">
                      <Input aria-label="修正集号" inputMode="numeric" value={activeDraft?.episode ?? ''} placeholder="未确定" onChange={(_, data) => onOverrideChange(activeIssue.evidence_id, { ...initialDraft, ...activeDraft, episode: data.value })} />
                      </Field>
                    </>
                  )}
                </div>
                </>)}
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" disabled={busy} onClick={() => setActiveIssue(null)}>取消</Button>
                {preview.status === 'confirmed' ? (
                  <Button appearance="primary" disabled={busy || !onReimport} onClick={() => { setActiveIssue(null); onReimport?.() }}>重新扫描此来源</Button>
                ) : identityIssue ? (
                  <Button appearance="primary" disabled={busy || !onOpenMaintenance} onClick={() => { setActiveIssue(null); onOpenMaintenance?.() }}>打开媒体库维护</Button>
                ) : (
                  <Button appearance="primary" disabled={busy || !activeDraft?.title.trim() || !activeDraft.mediaType || !hasValidTvNumbers} onClick={() => {
                    if (!activeDraft) return
                    void Promise.resolve(onApplyOverride(activeIssue.evidence_id, activeDraft)).then((ok) => { if (ok) setActiveIssue(null) })
                  }}>应用修正</Button>
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

export function V4RecognitionSummary({ preview, evidenceEntries, overrideDrafts, busy, onOverrideChange, onApplyOverride, onOpenMaintenance, onReimport }: V4RecognitionSummaryProps) {
  const summary = buildWorkSummaries(preview)
  const cardProps = {
    preview,
    evidenceEntries,
    overrideDrafts,
    busy,
    onOverrideChange,
    onApplyOverride,
    onOpenMaintenance,
    onReimport,
  }
  return (
    <div className="media-v4-recognition-summary">
      <div className="media-v4-summary-numbers">
        <div><strong>{summary.totalWorks}</strong><span>部作品</span></div>
        <div><strong>{summary.totalFiles}</strong><span>个视频</span></div>
        <div><strong>{summary.totalVideos}</strong><span>条剧集记录</span></div>
        <div className={summary.totalIssues > 0 ? 'attention' : ''}><strong>{summary.totalIssues}</strong><span>项需处理问题</span></div>
      </div>
      {(summary.unknownTypeWorks > 0 || summary.totalUnresolvedEpisodes > 0) && (
        <p className="media-v4-summary-unknown" role="status">
          {summary.unknownTypeWorks > 0 && `类型未定 ${summary.unknownTypeWorks} 部`}
          {summary.unknownTypeWorks > 0 && summary.totalUnresolvedEpisodes > 0 && ' · '}
          {summary.totalUnresolvedEpisodes > 0 && `集号未定 ${summary.totalUnresolvedEpisodes} 项`}
          {'。可先建立媒体库，稍后核对。'}
        </p>
      )}
      {summary.anomalyWorks.length > 0 && (
        <div className="media-v4-issues-block">
          <h3>需要处理</h3>
          <div className="media-v4-work-grid">
            {summary.anomalyWorks.map((work) => (
              <WorkCard key={work.work_key} work={work} defaultExpanded={false} {...cardProps} />
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
