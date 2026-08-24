/**
 * P-003 第二步：V4 识别结果的可审核紧凑摘要。
 *
 * 只消费 V4Preview；默认按作品卡展示，季度/电影/特别篇为次级层级，默认折叠；
 * 存在 issue 或集号跨度异常的作品置顶并展开。真正的人工修正仍通过
 * V4 override API，本组件不自行改写任何身份。
 */

import { useState } from 'react'
import { Button, Input, Select } from '@fluentui/react-components'
import { ChevronDown24Regular, ChevronRight24Regular, Warning24Regular } from '@fluentui/react-icons/fonts'
import type { V4Preview, V4ReviewIssue } from '../../api/mediaV4'
import { buildWorkSummaries, type RecognitionSummary, type WorkSummary } from '../../lib/mediaSummary'

export interface OverrideDraft {
  title: string
  mediaType: 'tv' | 'movie'
  season: string
  episode: string
}

export interface V4RecognitionSummaryProps {
  preview: V4Preview
  issues: V4ReviewIssue[]
  overrideDrafts: Record<string, OverrideDraft>
  busy: boolean
  onOverrideChange: (evidenceId: string, draft: OverrideDraft) => void
  onApplyOverride: (evidenceId: string) => void
}

function WorkCard({
  work,
  defaultExpanded,
  issues,
  overrideDrafts,
  busy,
  onOverrideChange,
  onApplyOverride,
}: {
  work: WorkSummary
  defaultExpanded: boolean
  issues: V4ReviewIssue[]
  overrideDrafts: Record<string, OverrideDraft>
  busy: boolean
  onOverrideChange: (evidenceId: string, draft: OverrideDraft) => void
  onApplyOverride: (evidenceId: string) => void
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [techOpen, setTechOpen] = useState(false)
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
                  <strong>{issue.code}</strong>
                  <span>{issue.message}</span>
                  <div className="media-v4-override-row">
                    <Input
                      aria-label="修正作品标题"
                      value={overrideDrafts[issue.evidence_id]?.title ?? ''}
                      placeholder="作品标题"
                      onChange={(_, data) => onOverrideChange(issue.evidence_id, { ...(overrideDrafts[issue.evidence_id] ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), title: data.value })}
                    />
                    <Select
                      aria-label="修正媒体类型"
                      value={overrideDrafts[issue.evidence_id]?.mediaType ?? 'tv'}
                      onChange={(event) => onOverrideChange(issue.evidence_id, { ...(overrideDrafts[issue.evidence_id] ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), mediaType: event.currentTarget.value as 'tv' | 'movie' })}
                    >
                      <option value="tv">剧集</option>
                      <option value="movie">电影</option>
                    </Select>
                    {((overrideDrafts[issue.evidence_id]?.mediaType ?? 'tv') === 'tv') && (
                      <>
                        <Input aria-label="修正季度" value={overrideDrafts[issue.evidence_id]?.season ?? '1'} placeholder="季度" onChange={(_, data) => onOverrideChange(issue.evidence_id, { ...(overrideDrafts[issue.evidence_id] ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), season: data.value })} />
                        <Input aria-label="修正集号" value={overrideDrafts[issue.evidence_id]?.episode ?? '1'} placeholder="集号" onChange={(_, data) => onOverrideChange(issue.evidence_id, { ...(overrideDrafts[issue.evidence_id] ?? { title: '', mediaType: 'tv', season: '1', episode: '1' }), episode: data.value })} />
                      </>
                    )}
                    <Button appearance="secondary" disabled={busy} onClick={() => onApplyOverride(issue.evidence_id)}>应用修正</Button>
                  </div>
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
              {issues.filter((issue) => issue.evidence_id && work.work_key).slice(0, 50).map((issue) => (
                <code key={`${issue.code}-${issue.evidence_id}`}>{issue.evidence_id} · {issue.message}</code>
              ))}
            </div>
          )}
        </div>
      )}
    </article>
  )
}

export function V4RecognitionSummary({ preview, issues, overrideDrafts, busy, onOverrideChange, onApplyOverride }: V4RecognitionSummaryProps) {
  const summary = buildWorkSummaries(preview)
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
              <WorkCard
                key={work.work_key}
                work={work}
                defaultExpanded
                issues={issues}
                overrideDrafts={overrideDrafts}
                busy={busy}
                onOverrideChange={onOverrideChange}
                onApplyOverride={onApplyOverride}
              />
            ))}
          </div>
        </div>
      )}
      <div className="media-v4-work-grid">
        {summary.normalWorks.map((work) => (
          <WorkCard
            key={work.work_key}
            work={work}
            defaultExpanded={false}
            issues={issues}
            overrideDrafts={overrideDrafts}
            busy={busy}
            onOverrideChange={onOverrideChange}
            onApplyOverride={onApplyOverride}
          />
        ))}
      </div>
      {summary.totalWorks === 0 && <div className="media-v4-empty">这个来源没有可建立媒体库的作品。</div>}
    </div>
  )
}

export type { RecognitionSummary }
