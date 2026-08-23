import { useEffect, useMemo, useState } from 'react'
import { Button, Checkbox, Dropdown, Field, Input, MessageBar, MessageBarBody, Option, Spinner } from '@fluentui/react-components'
import {
  ArrowReset24Regular,
  ArrowSync24Regular,
  CheckmarkCircle24Filled,
  CheckmarkCircle24Regular,
  Cloud24Regular,
  Database24Regular,
  DocumentText24Regular,
  Folder24Regular,
  FolderOpen24Regular,
  ScanObject24Regular,
} from '@fluentui/react-icons'
import { mediaV4Api, type V4Job, type V4Preview, type V4SourceEvidence } from '../api/mediaV4'
import { pickDirectoryTreeFile, pickFolder } from '../platform/folderPicker'
import { useMediaWorkflowStore } from '../stores/mediaWorkflow'

type ImportKind = 'local' | 'tree' | 'openlist' | 'hybrid'
type OverrideDraft = { title: string; mediaType: 'tv' | 'movie'; season: string; episode: string }

const ACTIVE_REVISION_KEY = 'kumiplayer.media-v4.active-revision'

const SOURCE_OPTIONS: Array<{
  kind: ImportKind
  label: string
  description: string
  icon: typeof Folder24Regular
}> = [
  { kind: 'local', label: '本地目录', description: '扫描电脑或已挂载网盘中的媒体文件', icon: Folder24Regular },
  { kind: 'tree', label: '目录树 TXT', description: '导入 115、百度或 OpenList 导出的目录清单', icon: DocumentText24Regular },
  { kind: 'openlist', label: 'OpenList', description: '直接扫描已配置的远端目录', icon: Cloud24Regular },
  { kind: 'hybrid', label: '目录树 + OpenList 增量', description: 'TXT 建立大库基线，OpenList 分批核对变化', icon: ArrowSync24Regular },
]

const PROVIDER_OPTIONS = [
  { value: 'local', label: '本地挂载' },
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
]

const JOB_LABELS: Record<string, string> = {
  materialize: '生成镜像文件',
  scrape: '获取媒体信息',
  projection: '更新媒体库',
  cleanup: '清理旧产物',
}

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '等待中',
  pending: '等待中',
  running: '正在处理',
  succeeded: '已完成',
  failed: '处理失败',
  cancelled: '已取消',
}

function createRevisionId() {
  return `rev-${crypto.randomUUID()}`
}

function episodeLabel(episode: V4Preview['episodes'][number]) {
  if (episode.season_kind === 'special') return `SP${String(episode.special_number || 1).padStart(2, '0')}`
  const season = episode.local_season_number == null ? '?' : String(episode.local_season_number).padStart(2, '0')
  const number = episode.local_episode_number == null ? '?' : String(episode.local_episode_number).padStart(2, '0')
  return `S${season}E${number}`
}

export default function MediaManagementPage() {
  const pendingDroppedTreePath = useMediaWorkflowStore((state) => state.pendingDroppedTreePath)
  const consumeDroppedTreePath = useMediaWorkflowStore((state) => state.consumeDroppedTreePath)
  const [kind, setKind] = useState<ImportKind>('local')
  const [path, setPath] = useState('')
  const [provider, setProvider] = useState('local')
  const [sourceRoot, setSourceRoot] = useState('')
  const [remoteRoot, setRemoteRoot] = useState('')
  const [fullScan, setFullScan] = useState(false)
  const [revisionId, setRevisionId] = useState('')
  const [scan, setScan] = useState<{
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    scan_mode?: 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
    scan_stats?: { requested_directories?: number; rolling_verified?: number; changed_directories?: number }
  } | null>(null)
  const [preview, setPreview] = useState<V4Preview | null>(null)
  const [jobs, setJobs] = useState<V4Job[]>([])
  const [busy, setBusy] = useState<'scan' | 'preview' | 'override' | 'confirm' | ''>('')
  const [error, setError] = useState('')
  const [allowEmpty, setAllowEmpty] = useState(false)
  const [overrideDrafts, setOverrideDrafts] = useState<Record<string, OverrideDraft>>({})

  useEffect(() => {
    if (!pendingDroppedTreePath) return
    setKind('tree')
    setPath(pendingDroppedTreePath)
    consumeDroppedTreePath()
  }, [consumeDroppedTreePath, pendingDroppedTreePath])

  useEffect(() => {
    const savedRevision = localStorage.getItem(ACTIVE_REVISION_KEY)
    if (!savedRevision) return
    setRevisionId(savedRevision)
    void mediaV4Api.status(savedRevision).then((result) => {
      setJobs(result.jobs)
    }).catch(() => {
      localStorage.removeItem(ACTIVE_REVISION_KEY)
    })
  }, [])

  useEffect(() => {
    if (!revisionId || jobs.length === 0 || jobs.every((job) => ['succeeded', 'failed', 'cancelled'].includes(job.status))) return
    const timer = window.setInterval(() => {
      void mediaV4Api.status(revisionId).then((result) => setJobs(result.jobs)).catch((cause) => {
        setError(cause instanceof Error ? cause.message : '后台任务状态读取失败')
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [jobs, revisionId])

  const groupedWorks = useMemo(() => {
    if (!preview) return []
    return preview.works.map((work) => ({
      ...work,
      episodes: preview.episodes.filter((episode) => episode.work_key === work.work_key),
      movieAssets: preview.work_assets.filter((asset) => asset.work_key === work.work_key),
    }))
  }, [preview])

  const activeStep = jobs.length > 0 ? 2 : scan ? 1 : 0
  const canScan = kind === 'openlist' || Boolean(path.trim())
  const providerLabel = PROVIDER_OPTIONS.find((option) => option.value === provider)?.label || provider

  const clearResultState = () => {
    setScan(null)
    setPreview(null)
    setJobs([])
    setRevisionId('')
    setError('')
    setAllowEmpty(false)
    setOverrideDrafts({})
    localStorage.removeItem(ACTIVE_REVISION_KEY)
  }

  const selectSourceKind = (nextKind: ImportKind) => {
    if (nextKind === kind) return
    setKind(nextKind)
    setPath('')
    setSourceRoot('')
    setRemoteRoot('')
    setFullScan(false)
    setProvider('local')
    clearResultState()
  }

  const choosePath = async () => {
    if (kind === 'openlist') return
    const selected = kind === 'local'
      ? await pickFolder(path, '选择本地媒体目录')
      : await pickDirectoryTreeFile(path, '选择目录树 TXT')
    if (selected) setPath(selected)
  }

  const chooseSourceRoot = async () => {
    const selected = await pickFolder(sourceRoot, '选择本地挂载根目录')
    if (selected) setSourceRoot(selected)
  }

  const scanSource = async () => {
    if (kind !== 'openlist' && !path.trim()) {
      setError(kind === 'local' ? '请先选择本地媒体目录' : '请先选择目录树 TXT')
      return
    }
    setBusy('scan')
    setError('')
    setPreview(null)
    setJobs([])
    const nextRevisionId = createRevisionId()
    setRevisionId(nextRevisionId)
    setOverrideDrafts({})
    localStorage.removeItem(ACTIVE_REVISION_KEY)
    try {
      const result = await mediaV4Api.scan({
        source: kind,
        root_path: kind === 'local' ? path : kind === 'openlist' ? path : kind === 'hybrid' ? remoteRoot : sourceRoot || 'tree',
        tree_file: kind === 'tree' || kind === 'hybrid' ? path : '',
        provider: kind === 'local' ? 'local' : provider,
        source_root: kind === 'tree' ? sourceRoot : '',
        scan_mode: kind === 'openlist' && fullScan ? 'full' : 'auto',
      })
      setScan(result)
      setAllowEmpty(false)
      if (result.entries.length > 0) {
        const previewResult = await mediaV4Api.preview({
          revision_id: nextRevisionId,
          root_id: result.root_id,
          scan_id: result.scan_id,
          entries: result.entries,
          allow_empty: false,
        })
        setPreview(previewResult)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '来源扫描失败')
    } finally {
      setBusy('')
    }
  }

  const buildPreview = async () => {
    if (!scan) return
    if (scan.entries.length === 0 && !allowEmpty) {
      setError('来源当前为空；必须明确确认后才能移除该来源先前导入的媒体')
      return
    }
    const nextRevisionId = revisionId || createRevisionId()
    setBusy('preview')
    setError('')
    try {
      const result = await mediaV4Api.preview({
        revision_id: nextRevisionId,
        root_id: scan.root_id,
        scan_id: scan.scan_id,
        entries: scan.entries,
        allow_empty: allowEmpty,
      })
      setRevisionId(nextRevisionId)
      setPreview(result)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '识别预览失败')
    } finally {
      setBusy('')
    }
  }

  const confirmRevision = async () => {
    if (!preview || preview.issues.length > 0 || !revisionId) return
    setBusy('confirm')
    setError('')
    try {
      const result = await mediaV4Api.confirm(revisionId)
      setJobs(result.jobs)
      localStorage.setItem(ACTIVE_REVISION_KEY, revisionId)
      setPreview({ ...preview, status: 'confirmed' })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '确认导入失败')
    } finally {
      setBusy('')
    }
  }

  const applyOverride = async (evidenceId: string) => {
    if (!revisionId) return
    const draft = overrideDrafts[evidenceId]
    if (!draft?.title.trim()) {
      setError('人工修正至少需要填写作品标题')
      return
    }
    const season = Number.parseInt(draft.season || '1', 10)
    const episode = Number.parseInt(draft.episode || '1', 10)
    setBusy('override')
    setError('')
    try {
      const result = await mediaV4Api.overrideEvidence(revisionId, evidenceId, {
        work_title: draft.title.trim(),
        title_candidates: [draft.title.trim()],
        media_type: draft.mediaType,
        group_type: draft.mediaType === 'movie' ? 'movie' : 'season',
        season_candidate: draft.mediaType === 'tv' && Number.isFinite(season) ? season : null,
        episode_candidate: draft.mediaType === 'tv' && Number.isFinite(episode) ? episode : null,
        needs_review: false,
        is_importable: true,
        is_auxiliary: false,
      })
      setPreview(result)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '人工修正失败')
    } finally {
      setBusy('')
    }
  }

  const startNewImport = () => {
    setKind('local')
    setPath('')
    setProvider('local')
    setSourceRoot('')
    setRemoteRoot('')
    setFullScan(false)
    clearResultState()
  }

  return (
    <div className="media-flow-page media-v4-page">
      <header className="media-flow-header">
        <div className="media-flow-title">
          <span>媒体管理</span>
          <h1>导入媒体</h1>
          <p>选择一个媒体来源，检查识别结果，然后建立可播放的媒体库。</p>
        </div>
        <Button className="media-v4-new-import" appearance="subtle" icon={<ArrowReset24Regular />} onClick={startNewImport}>新建导入</Button>
      </header>

      <nav className="media-v4-steps" aria-label="导入步骤">
        <ol>
          {['选择来源', '检查识别', '建立媒体库'].map((label, index) => (
            <li className={index < activeStep ? 'complete' : index === activeStep ? 'active' : ''} key={label} aria-current={index === activeStep ? 'step' : undefined}>
              <span className="media-v4-step-index" aria-hidden="true">{index < activeStep ? <CheckmarkCircle24Filled /> : index + 1}</span>
              <span>{label}</span>
            </li>
          ))}
        </ol>
      </nav>

      {error && <MessageBar className="media-v4-message" intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      <section className="media-stage-shell media-v4-stage-panel media-v4-source-card">
        <div className="media-stage-header">
          <div className="media-stage-heading">
            <span className="media-stage-icon" aria-hidden="true"><FolderOpen24Regular /></span>
            <div>
              <span className="media-stage-eyebrow">第 1 步</span>
              <h2>选择媒体来源</h2>
              <p>四种入口使用同一套识别规则，不会移动或改名你的原始媒体文件。</p>
            </div>
          </div>
        </div>

        <div className="media-v4-source-options" role="group" aria-label="媒体来源类型">
          {SOURCE_OPTIONS.map((option) => {
            const Icon = option.icon
            const selected = kind === option.kind
            return (
              <button type="button" className={selected ? 'selected' : ''} aria-label={option.label} aria-pressed={selected} onClick={() => selectSourceKind(option.kind)} key={option.kind}>
                <span className="media-v4-source-icon" aria-hidden="true"><Icon /></span>
                <span><strong>{option.label}</strong><small>{option.description}</small></span>
                <span className="media-v4-source-check" aria-hidden="true">{selected && <CheckmarkCircle24Filled />}</span>
              </button>
            )
          })}
        </div>

        <div className="media-v4-config-panel">
          <div className="media-v4-config-heading">
            <strong>{kind === 'local' ? '配置本地目录' : kind === 'tree' ? '配置目录树清单' : kind === 'hybrid' ? '配置 TXT 基线与 OpenList 增量' : '配置 OpenList 扫描'}</strong>
            <span>{kind === 'local' ? '支持直接输入路径，也可以从资源管理器选择。' : kind === 'tree' ? 'TXT 只作为目录证据；可选挂载目录用于生成可播放路径。' : kind === 'hybrid' ? '首次只读取 TXT；确认后，同一远端目录会按预算核对变化，避免每次遍历整库。' : '留空会扫描已配置的 OpenList 根目录。'}</span>
          </div>

          <div className={`media-v4-controls media-v4-controls-${kind}`}>
            {kind !== 'local' && (
              <Field label="默认内容来源" className="media-v4-provider-field">
                <Dropdown aria-label="默认内容来源" value={providerLabel} selectedOptions={[provider]} onOptionSelect={(_, data) => data.optionValue && setProvider(data.optionValue)}>
                  {PROVIDER_OPTIONS.map((option) => <Option key={option.value} value={option.value}>{option.label}</Option>)}
                </Dropdown>
              </Field>
            )}

            <Field label={kind === 'local' ? '媒体目录' : kind === 'tree' ? '目录树 TXT 文件' : kind === 'hybrid' ? '首次目录树 TXT 文件' : 'OpenList 远端目录'} className="media-v4-path-field">
              <div className="media-v4-path-row">
                <Input
                  aria-label={kind === 'local' ? '媒体目录' : kind === 'tree' ? '目录树 TXT 文件' : kind === 'hybrid' ? '首次目录树 TXT 文件' : 'OpenList 远端目录'}
                  value={path}
                  onChange={(_, data) => setPath(data.value)}
                  placeholder={kind === 'local' ? '选择或输入本地媒体目录' : kind === 'tree' || kind === 'hybrid' ? '选择或输入目录树 TXT 路径' : '/动画（留空扫描根目录）'}
                />
                {kind !== 'openlist' && <Button appearance="secondary" icon={<FolderOpen24Regular />} onClick={() => void choosePath()}>浏览</Button>}
              </div>
            </Field>

            {kind === 'tree' && (
              <Field label="本地挂载根目录（可选）" className="media-v4-mount-field">
                <div className="media-v4-path-row">
                  <Input aria-label="本地挂载根目录（可选）" value={sourceRoot} onChange={(_, data) => setSourceRoot(data.value)} placeholder="用于把相对路径映射到播放位置" />
                  <Button appearance="secondary" icon={<FolderOpen24Regular />} onClick={() => void chooseSourceRoot()}>浏览</Button>
                </div>
              </Field>
            )}

            {kind === 'hybrid' && (
              <Field label="OpenList 增量目录" className="media-v4-mount-field" hint="必须与 TXT 内容对应；留空使用 OpenList 设置中的远端根目录。">
                <Input aria-label="OpenList 增量目录" value={remoteRoot} onChange={(_, data) => setRemoteRoot(data.value)} placeholder="/115网盘/动画（可留空）" />
              </Field>
            )}

            {kind === 'openlist' && (
              <div className="media-v4-scan-mode media-v4-mount-field">
                <Checkbox
                  checked={fullScan}
                  onChange={(_, data) => setFullScan(Boolean(data.checked))}
                  label="本次进行完整远端校验（请求较多）"
                />
                <span>默认优先使用已确认的 TXT 基线做风险受控更新；怀疑遗漏变化时再启用完整校验。</span>
              </div>
            )}
          </div>

          <div className="media-v4-command-row">
            <div><strong>准备扫描</strong><span>{canScan ? '扫描后会直接生成可审核的识别结果。' : '请先填写或选择媒体目录。'}</span></div>
            <Button aria-label="扫描并识别" className="media-primary-command" appearance="primary" icon={<ScanObject24Regular />} disabled={busy !== '' || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : '扫描并识别'}</Button>
          </div>
        </div>
      </section>

      {scan && <section className="media-stage-shell media-v4-stage-panel media-v4-review-card">
        <div className="media-stage-header">
          <div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><CheckmarkCircle24Regular /></span><div><span className="media-stage-eyebrow">第 2 步</span><h2>检查识别结果</h2><p>已扫描 {scan.entries.length} 个媒体条目。确认前可以检查作品、季度、剧集和文件版本。</p></div></div>
          {!preview && <Button appearance="secondary" disabled={busy !== '' || (scan.entries.length === 0 && !allowEmpty)} onClick={() => void buildPreview()}>{busy === 'preview' ? <Spinner size="tiny" /> : '生成识别预览'}</Button>}
        </div>
        {scan.scan_mode === 'incremental' && <MessageBar intent="info"><MessageBarBody>本次使用 OpenList 增量核对：请求 {scan.scan_stats?.requested_directories || 0} 个目录，其中滚动抽查 {scan.scan_stats?.rolling_verified || 0} 个、变化优先核对 {scan.scan_stats?.changed_directories || 0} 个。</MessageBarBody></MessageBar>}
        {scan.scan_mode === 'tree_baseline' && <MessageBar intent="info"><MessageBarBody>TXT 基线已建立。确认本次导入后，再扫描同一 OpenList 目录时会自动进入风险受控增量核对。</MessageBarBody></MessageBar>}
        {!preview && <div className="media-v4-empty">{scan.entries.length === 0 ? <Checkbox checked={allowEmpty} onChange={(_, data) => setAllowEmpty(Boolean(data.checked))} label="我确认该来源当前确实为空，并允许移除它先前导入的媒体" /> : '正在生成识别结果…'}</div>}
        {preview && <>
          {preview.issues.length > 0 && <MessageBar intent="warning"><MessageBarBody>发现 {preview.issues.length} 个需要人工处理的问题；未解决前不能确认。</MessageBarBody></MessageBar>}
          {groupedWorks.length > 0 ? <div className="media-v4-work-grid">{groupedWorks.map((work) => <article className="media-v4-work-card" key={work.work_key}><div><strong>{work.preferred_title || '未命名作品'}</strong><span>{work.year || '年份未知'} · {work.media_type === 'movie' ? '电影' : '剧集'}</span></div><div className="media-v4-episode-list">{work.episodes.map((episode) => <span key={`${episode.episode_key}-${episode.edition_key}`}>{episodeLabel(episode)} · {episode.asset_evidence_ids.length} 个文件</span>)}{work.movieAssets.map((asset) => <span key={`${work.work_key}-${asset.edition_key}`}>电影{asset.edition_key === 'default' ? '' : ` · ${asset.edition_key}`} · {asset.asset_evidence_ids.length} 个文件</span>)}</div></article>)}</div> : <div className="media-v4-empty">这个来源没有可建立媒体库的作品。</div>}
          {preview.issues.length > 0 && <div className="media-v4-issues">{preview.issues.map((issue) => { const draft = overrideDrafts[issue.evidence_id] || { title: '', mediaType: 'tv' as const, season: '1', episode: '1' }; return <div key={`${issue.code}-${issue.evidence_id}`}><strong>{issue.code}</strong><span>{issue.message}</span><div className="media-v4-override-row"><Input aria-label="修正作品标题" value={draft.title} placeholder="作品标题" onChange={(_, data) => setOverrideDrafts((current) => ({ ...current, [issue.evidence_id]: { ...draft, title: data.value } }))} /><Dropdown aria-label="修正媒体类型" value={draft.mediaType === 'movie' ? '电影' : '剧集'} selectedOptions={[draft.mediaType]} onOptionSelect={(_, data) => data.optionValue && setOverrideDrafts((current) => ({ ...current, [issue.evidence_id]: { ...draft, mediaType: data.optionValue as 'tv' | 'movie' } }))}><Option value="tv">剧集</Option><Option value="movie">电影</Option></Dropdown>{draft.mediaType === 'tv' && <><Input aria-label="修正季度" value={draft.season} placeholder="季度" onChange={(_, data) => setOverrideDrafts((current) => ({ ...current, [issue.evidence_id]: { ...draft, season: data.value } }))} /><Input aria-label="修正集号" value={draft.episode} placeholder="集号" onChange={(_, data) => setOverrideDrafts((current) => ({ ...current, [issue.evidence_id]: { ...draft, episode: data.value } }))} /></>}<Button appearance="secondary" disabled={busy !== ''} onClick={() => void applyOverride(issue.evidence_id)}>应用修正</Button></div></div> })}</div>}
          <div className="media-v4-command-row media-v4-confirm-row"><div><strong>{preview.issues.length > 0 ? '需要先处理识别问题' : '识别结果可以建立媒体库'}</strong><span>确认后将生成镜像、获取媒体信息并更新媒体库。</span></div><Button className="media-primary-command" appearance="primary" icon={<Database24Regular />} disabled={busy !== '' || preview.issues.length > 0 || preview.status === 'confirmed'} onClick={() => void confirmRevision()}>{busy === 'confirm' ? <><Spinner size="tiny" />正在建立</> : preview.status === 'confirmed' ? '已建立媒体库' : '确认并建立媒体库'}</Button></div>
        </>}
      </section>}

      {jobs.length > 0 && <section className="media-stage-shell media-v4-stage-panel media-v4-jobs-card"><div className="media-stage-header"><div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><Database24Regular /></span><div><span className="media-stage-eyebrow">第 3 步</span><h2>建立媒体库</h2><p>可以离开此页面；返回后会继续显示当前导入进度。</p></div></div></div><div className="media-v4-job-list">{jobs.map((job) => <article className={`media-v4-job media-v4-job-${job.status}`} key={job.job_id}><span className="media-v4-job-state" aria-hidden="true">{job.status === 'succeeded' ? <CheckmarkCircle24Filled /> : <span />}</span><div><strong>{JOB_LABELS[job.job_type] || job.job_type}</strong><span>{JOB_STATUS_LABELS[job.status] || job.status}{job.attempts ? ` · 第 ${job.attempts} 次尝试` : ''}</span>{job.last_error && <span className="media-v4-job-error" role="alert">{job.last_error}</span>}</div><code title={job.job_id}>{job.job_id}</code></article>)}</div></section>}
    </div>
  )
}
