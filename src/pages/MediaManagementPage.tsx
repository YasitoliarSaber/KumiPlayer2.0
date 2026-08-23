import { useEffect, useMemo, useState } from 'react'
import { Button, Field, Input, MessageBar, MessageBarBody, Spinner } from '@fluentui/react-components'
import { CheckmarkCircle24Regular, FolderOpen24Regular, ArrowReset24Regular } from '@fluentui/react-icons'
import { mediaV4Api, type V4Job, type V4Preview, type V4SourceEvidence } from '../api/mediaV4'
import { pickDirectoryTreeFile, pickFolder } from '../platform/folderPicker'
import { useMediaWorkflowStore } from '../stores/mediaWorkflow'

type ImportKind = 'local' | 'tree'

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
  const [revisionId, setRevisionId] = useState('')
  const [scan, setScan] = useState<{ root_id: string; scan_id: string; entries: V4SourceEvidence[] } | null>(null)
  const [preview, setPreview] = useState<V4Preview | null>(null)
  const [jobs, setJobs] = useState<V4Job[]>([])
  const [busy, setBusy] = useState<'scan' | 'preview' | 'confirm' | ''>('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!pendingDroppedTreePath) return
    setKind('tree')
    setPath(pendingDroppedTreePath)
    consumeDroppedTreePath()
  }, [consumeDroppedTreePath, pendingDroppedTreePath])

  const groupedWorks = useMemo(() => {
    if (!preview) return []
    return preview.works.map((work) => ({
      ...work,
      episodes: preview.episodes.filter((episode) => episode.work_key === work.work_key),
    }))
  }, [preview])

  const choosePath = async () => {
    const selected = kind === 'local'
      ? await pickFolder(path, '选择本地媒体目录')
      : await pickDirectoryTreeFile(path, '选择目录树 TXT')
    if (selected) setPath(selected)
  }

  const scanSource = async () => {
    if (!path.trim()) {
      setError(kind === 'local' ? '请先选择本地媒体目录' : '请先选择目录树 TXT')
      return
    }
    setBusy('scan')
    setError('')
    setPreview(null)
    setJobs([])
    try {
      const result = await mediaV4Api.scan({
        source: kind,
        root_path: kind === 'local' ? path : sourceRoot || 'tree',
        tree_file: kind === 'tree' ? path : '',
        provider: kind === 'tree' ? provider : 'local',
        source_root: sourceRoot,
      })
      setScan(result)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '来源扫描失败')
    } finally {
      setBusy('')
    }
  }

  const buildPreview = async () => {
    if (!scan) return
    const nextRevisionId = revisionId || createRevisionId()
    setBusy('preview')
    setError('')
    try {
      const result = await mediaV4Api.preview({
        revision_id: nextRevisionId,
        root_id: scan.root_id,
        scan_id: scan.scan_id,
        entries: scan.entries,
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
      setPreview({ ...preview, status: 'confirmed' })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '确认导入失败')
    } finally {
      setBusy('')
    }
  }

  const reset = () => {
    setScan(null)
    setPreview(null)
    setJobs([])
    setRevisionId('')
    setError('')
  }

  return (
    <div className="media-flow-page media-v4-page">
      <header className="media-flow-header">
        <div>
          <span className="media-stage-eyebrow">MEDIA V4</span>
          <h1>导入媒体</h1>
          <p>扫描只记录来源事实；确认后由同一个 revision 驱动镜像、刮削和媒体库投影。</p>
        </div>
        <Button appearance="subtle" icon={<ArrowReset24Regular />} onClick={reset}>新建导入</Button>
      </header>

      {error && <MessageBar intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      <section className="media-stage-shell media-v4-source-card">
        <div className="media-stage-header">
          <div className="media-stage-heading">
            <span className="media-stage-icon" aria-hidden="true"><FolderOpen24Regular /></span>
            <div>
              <span className="media-stage-eyebrow">SOURCE EVIDENCE</span>
              <h2>选择来源</h2>
              <p>本地目录和目录树 TXT 都先转换成统一的来源证据，再进入同一套识别规则。</p>
            </div>
          </div>
        </div>
        <div className="media-v4-controls">
          <Field label="导入入口">
            <select value={kind} onChange={(event) => { setKind(event.target.value as ImportKind); reset() }}>
              <option value="local">本地媒体目录</option>
              <option value="tree">目录树 TXT（115 / 百度 / OpenList）</option>
            </select>
          </Field>
          {kind === 'tree' && <Field label="远程来源"><select value={provider} onChange={(event) => setProvider(event.target.value)}><option value="local">本地挂载</option><option value="pan115">115</option><option value="baidu">百度网盘</option><option value="openlist">OpenList</option></select></Field>}
          <Field label={kind === 'local' ? '媒体目录' : '目录树 TXT'} className="media-v4-path-field">
            <div className="media-v4-path-row"><Input value={path} onChange={(_, data) => setPath(data.value)} placeholder={kind === 'local' ? '选择或输入本地媒体目录' : '选择或输入目录树 TXT 路径'} /><Button appearance="secondary" icon={<FolderOpen24Regular />} onClick={() => void choosePath()}>浏览</Button></div>
          </Field>
          {kind === 'tree' && <Field label="本地挂载根目录（可选）"><Input value={sourceRoot} onChange={(_, data) => setSourceRoot(data.value)} placeholder="用于把相对路径映射到播放位置" /></Field>}
        </div>
        <div className="media-stage-actions"><Button appearance="primary" disabled={busy !== ''} onClick={() => void scanSource()}>{busy === 'scan' ? <Spinner size="tiny" /> : '扫描来源'}</Button></div>
      </section>

      {scan && <section className="media-stage-shell media-v4-review-card">
        <div className="media-stage-header"><div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><CheckmarkCircle24Regular /></span><div><span className="media-stage-eyebrow">PARSED FACTS</span><h2>识别预览</h2><p>共记录 {scan.entries.length} 个来源条目；作品和剧集身份还没有进入执行阶段。</p></div></div><Button appearance="secondary" disabled={busy !== ''} onClick={() => void buildPreview()}>{busy === 'preview' ? <Spinner size="tiny" /> : '生成预览'}</Button></div>
        {!preview && <div className="media-v4-empty">点击“生成预览”开始识别。</div>}
        {preview && <>
          {preview.issues.length > 0 && <MessageBar intent="warning"><MessageBarBody>发现 {preview.issues.length} 个需要人工处理的问题；未解决前不能确认。</MessageBarBody></MessageBar>}
          <div className="media-v4-work-grid">{groupedWorks.map((work) => <article className="media-v4-work-card" key={work.work_key}><div><strong>{work.preferred_title || '未命名作品'}</strong><span>{work.year || '年份未知'} · {work.media_type}</span></div><div className="media-v4-episode-list">{work.episodes.map((episode) => <span key={episode.episode_key}>{episodeLabel(episode)} · {episode.asset_evidence_ids.length} 个版本</span>)}</div></article>)}</div>
          {preview.issues.length > 0 && <div className="media-v4-issues">{preview.issues.map((issue) => <div key={`${issue.code}-${issue.evidence_id}`}><strong>{issue.code}</strong><span>{issue.message}</span></div>)}</div>}
          <div className="media-stage-actions"><Button appearance="primary" disabled={busy !== '' || preview.issues.length > 0 || preview.status === 'confirmed'} onClick={() => void confirmRevision()}>{busy === 'confirm' ? <Spinner size="tiny" /> : preview.status === 'confirmed' ? '已确认' : '确认并建立媒体库'}</Button></div>
        </>}
      </section>}

      {jobs.length > 0 && <section className="media-stage-shell media-v4-jobs-card"><div className="media-stage-header"><div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><CheckmarkCircle24Regular /></span><div><span className="media-stage-eyebrow">CONFIRMED REVISION</span><h2>后台任务</h2><p>任务只引用 revision、work 和 asset，不会重新识别来源。</p></div></div></div><div className="media-v4-job-list">{jobs.map((job) => <div key={job.job_id}><strong>{job.job_type}</strong><span>{job.status}</span><code>{job.job_id}</code></div>)}</div></section>}
    </div>
  )
}
