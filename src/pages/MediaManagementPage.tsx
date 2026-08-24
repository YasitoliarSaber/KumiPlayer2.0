import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Checkbox, Input, MessageBar, MessageBarBody, Select, Spinner } from '@fluentui/react-components'
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
import { mediaV4Api, type V4Job, type V4OpenlistBaselineStatus, type V4Preview, type V4SourceEvidence, type V4SourceLibraryCard } from '../api/mediaV4'
import type { V4ExecutionProgress } from '../api/mediaV4'
import { configApi, type PublicConfig } from '../api/config'
import { openlistApi } from '../api/openlist'
import { tasksApi } from '../api/tasks'
import type { OpenListRoute, ProviderId } from '../api/types'
import OpenListFolderBrowser from '../components/media/OpenListFolderBrowser'
import { V4ExecutionProgress as V4ExecutionProgressView } from '../components/media/V4ExecutionProgress'
import { V4RecognitionSummary, type OverrideDraft } from '../components/media/V4RecognitionSummary'
import { pickDirectoryTreeFile, pickFolder } from '../platform/folderPicker'
import { useMediaWorkflowStore } from '../stores/mediaWorkflow'
import { useUiStore } from '../stores/ui'

type ImportKind = 'local' | 'tree' | 'openlist' | 'hybrid'
type WorkflowStage = 'source' | 'review' | 'execute'
type SourceCardMetadata = {
  source_display_name: string
  source_locator: string
  playback_locator: string
  source_route_id: string
}

const ACTIVE_REVISION_KEY = 'kumiplayer.media-v4.active-revision'

const SOURCE_OPTIONS: Array<{
  kind: ImportKind
  label: string
  description: string
  icon: typeof Folder24Regular
}> = [
  { kind: 'local', label: '本地目录', description: '仅扫描本机物理磁盘中的媒体文件', icon: Folder24Regular },
  { kind: 'tree', label: '目录树 TXT', description: '导入 115、百度或 OpenList 导出的目录清单', icon: DocumentText24Regular },
  { kind: 'openlist', label: 'OpenList', description: '直接扫描已配置的远端目录', icon: Cloud24Regular },
  { kind: 'hybrid', label: '目录树 + OpenList 增量', description: 'TXT 建立大库基线，OpenList 分批核对变化', icon: ArrowSync24Regular },
]

const PROVIDER_OPTIONS: Array<{
  value: Exclude<ProviderId, 'local' | 'other'>
  label: string
  mark: string
  website: string
  websiteLabel: string
}> = [
  { value: 'pan115', label: '115 网盘', mark: '115', website: 'https://115.com/', websiteLabel: '前往 115 官网生成目录树' },
  { value: 'baidu', label: '百度网盘', mark: '百', website: 'https://pan.baidu.com/', websiteLabel: '前往百度网盘官网' },
  { value: 'quark', label: '夸克网盘', mark: '夸', website: 'https://pan.quark.cn/', websiteLabel: '前往夸克网盘官网' },
]

const JOB_LABELS: Record<string, string> = {
  materialize_mirror: '生成镜像文件',
  scrape_work: '获取媒体信息',
  refresh_projection: '更新媒体库',
  cleanup_superseded_artifacts: '清理旧版本',
}

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '等待中',
  pending: '等待中',
  running: '正在处理',
  succeeded: '已完成',
  failed: '处理失败',
  cancelled: '已取消',
}

const IMPORT_STEPS = [
  { label: '选择来源', icon: FolderOpen24Regular },
  { label: '检查识别', icon: ScanObject24Regular },
  { label: '建立媒体库', icon: Database24Regular },
]

function createRevisionId() {
  return `rev-${crypto.randomUUID()}`
}

function routeForPath(routes: OpenListRoute[], remotePath: string) {
  const normalized = (remotePath || '/').replace(/\\/g, '/').replace(/\/+$/, '') || '/'
  return routes
    .filter((route) => route.enabled && (normalized === route.remote_prefix || normalized.startsWith(`${route.remote_prefix}/`)))
    .sort((left, right) => right.remote_prefix.length - left.remote_prefix.length)[0]
}

function playbackRootForRoute(route: OpenListRoute | undefined, remotePath: string) {
  if (!route?.local_path) return ''
  const normalizedPath = (remotePath || '/').replace(/\\/g, '/').replace(/\/+$/, '') || '/'
  const normalizedPrefix = (route.remote_prefix || '/').replace(/\\/g, '/').replace(/\/+$/, '') || '/'
  const relative = normalizedPath === normalizedPrefix
    ? ''
    : normalizedPath.slice(normalizedPrefix.length).replace(/^\/+/, '')
  if (!relative) return route.local_path
  const separator = route.local_path.includes('\\') ? '\\' : '/'
  return `${route.local_path.replace(/[\\/]+$/, '')}${separator}${relative.replace(/\//g, separator)}`
}

function finalPathSegment(path: string) {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts.at(-1) || ''
}

function ProviderPicker({ value, onChange }: {
  value: Exclude<ProviderId, 'local' | 'other'>
  onChange: (provider: Exclude<ProviderId, 'local' | 'other'>) => void
}) {
  return (
    <div className="media-v4-provider-picker" role="group" aria-label="内容来源">
      {PROVIDER_OPTIONS.map((option) => (
        <button
          type="button"
          className={value === option.value ? 'selected' : ''}
          aria-label={option.label}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          key={option.value}
        >
          <span className={`media-v4-provider-mark provider-${option.value}`} aria-hidden="true">{option.mark}</span>
          <span><strong>{option.label}</strong><small>目录树中的实际内容来源</small></span>
          {value === option.value && <CheckmarkCircle24Filled aria-hidden="true" />}
        </button>
      ))}
    </div>
  )
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
  const goSettings = useUiStore((state) => state.goSettings)
  const [kind, setKind] = useState<ImportKind>('local')
  const [path, setPath] = useState('')
  const [provider, setProvider] = useState<Exclude<ProviderId, 'local' | 'other'>>('pan115')
  const [remoteRoot, setRemoteRoot] = useState('')
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [routes, setRoutes] = useState<OpenListRoute[]>([])
  const [remoteBrowsing, setRemoteBrowsing] = useState(false)
  const [browserSession, setBrowserSession] = useState(0)
  const [openlistBaseline, setOpenlistBaseline] = useState<V4OpenlistBaselineStatus | null>(null)
  const [sourceCards, setSourceCards] = useState<V4SourceLibraryCard[]>([])
  const [sourceCardsLoading, setSourceCardsLoading] = useState(true)
  const [revisionId, setRevisionId] = useState('')
  const [workflowStage, setWorkflowStage] = useState<WorkflowStage>('source')
  const [executeProgress, setExecuteProgress] = useState<V4ExecutionProgress | null>(null)
  const [scan, setScan] = useState<{
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    scan_mode?: 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
    source_mode?: string
    scan_stats?: { requested_directories?: number; rolling_verified?: number; changed_directories?: number }
    source_metadata: SourceCardMetadata
  } | null>(null)
  const [preview, setPreview] = useState<V4Preview | null>(null)
  const [jobs, setJobs] = useState<V4Job[]>([])
  const [busy, setBusy] = useState<'scan' | 'preview' | 'override' | 'confirm' | ''>('')
  const [retryingJobId, setRetryingJobId] = useState('')
  const [error, setError] = useState('')
  const [allowEmpty, setAllowEmpty] = useState(false)
  const [overrideDrafts, setOverrideDrafts] = useState<Record<string, OverrideDraft>>({})
  const sourceCardsRefreshInFlight = useRef(false)

  useEffect(() => {
    let alive = true
    void Promise.all([
      configApi.getConfig(),
      openlistApi.getRoutes().catch(() => ({ routes: [] as OpenListRoute[] })),
    ]).then(([nextConfig, routeResult]) => {
      if (!alive) return
      setConfig(nextConfig)
      setRoutes(routeResult.routes.length > 0 ? routeResult.routes : nextConfig.openlist_routes || [])
      setPath((current) => current || nextConfig.local_root || '')
      setRemoteRoot((current) => current || nextConfig.openlist_remote_root || '/')
    }).catch((cause) => {
      if (alive) setError(cause instanceof Error ? cause.message : '无法读取媒体来源设置')
    })
    return () => { alive = false }
  }, [])

  const refreshSourceCards = useCallback(async () => {
    if (sourceCardsRefreshInFlight.current) return
    sourceCardsRefreshInFlight.current = true
    setSourceCardsLoading(true)
    try {
      const result = await mediaV4Api.sourceLibraries()
      setSourceCards(result.cards)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法读取已导入媒体库')
    } finally {
      sourceCardsRefreshInFlight.current = false
      setSourceCardsLoading(false)
    }
  }, [])

  const refreshOpenlistBaseline = useCallback(async (remote: string) => {
    try {
      const status = await mediaV4Api.openlistStatus(remote)
      setOpenlistBaseline(status)
    } catch {
      // 凭据/连接未就绪时不阻塞浏览；扫描动作会给出可操作错误。
      setOpenlistBaseline(null)
    }
  }, [])

  useEffect(() => {
    if ((kind !== 'openlist' && kind !== 'hybrid') || !remoteRoot.trim()) return
    void refreshOpenlistBaseline(remoteRoot)
  }, [kind, remoteRoot, refreshOpenlistBaseline])

  useEffect(() => {
    void refreshSourceCards()
  }, [refreshSourceCards])

  const hasActiveSourceJobs = sourceCards.some((card) => card.job_summary.queued > 0 || card.job_summary.running > 0)
  useEffect(() => {
    if (!hasActiveSourceJobs) return
    let cancelled = false
    let timer = 0
    const poll = async () => {
      await refreshSourceCards()
      if (!cancelled) timer = window.setTimeout(() => { void poll() }, 1500)
    }
    timer = window.setTimeout(() => { void poll() }, 1500)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [hasActiveSourceJobs, refreshSourceCards])

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
      if (result.progress) setExecuteProgress(result.progress)
      setWorkflowStage('execute')
    }).catch(() => {
      localStorage.removeItem(ACTIVE_REVISION_KEY)
    })
  }, [])

  const hasActiveJobs = jobs.some((job) => !['succeeded', 'failed', 'cancelled'].includes(job.status))
  const executionActive = executeProgress?.overall_status === 'running' || executeProgress?.overall_status === 'queued'
  useEffect(() => {
    if (!revisionId || !hasActiveJobs) return
    let cancelled = false
    let timer = 0
    const poll = async () => {
      try {
        const result = await mediaV4Api.status(revisionId)
        if (!cancelled) {
          setJobs(result.jobs)
          if (result.progress) setExecuteProgress(result.progress)
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : '后台任务状态读取失败')
      } finally {
        if (!cancelled) timer = window.setTimeout(() => { void poll() }, 1000)
      }
    }
    timer = window.setTimeout(() => { void poll() }, 1000)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [hasActiveJobs, revisionId])

  useEffect(() => {
    if (!revisionId || !executionActive) return
    let cancelled = false
    let timer = 0
    const poll = async () => {
      try {
        const result = await mediaV4Api.status(revisionId)
        if (!cancelled) {
          setJobs(result.jobs)
          if (result.progress) setExecuteProgress(result.progress)
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : '后台任务状态读取失败')
      } finally {
        if (!cancelled) timer = window.setTimeout(() => { void poll() }, 1200)
      }
    }
    timer = window.setTimeout(() => { void poll() }, 1200)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [executionActive, revisionId])

  const activeStep = workflowStage === 'source' ? 0 : workflowStage === 'review' ? 1 : 2
  const selectedRemoteRoute = routeForPath(routes, remoteRoot)
  const canScan = kind === 'openlist'
    ? Boolean(config?.openlist_configured && remoteRoot && selectedRemoteRoute?.local_path)
    : kind === 'hybrid'
      ? Boolean(path.trim() && config?.openlist_configured && remoteRoot && selectedRemoteRoute?.local_path)
      : kind === 'tree'
        ? Boolean(path.trim() && providerRoot(provider))
        : Boolean(path.trim())
  const showReset = workflowStage !== 'source' || kind !== 'local' || Boolean(error)

  function providerRoot(providerId: ProviderId, remotePath = '') {
    const matchedRoute = routeForPath(routes, remotePath)
    if (matchedRoute?.provider_id === providerId) return playbackRootForRoute(matchedRoute, remotePath)
    if (providerId === 'pan115') return config?.pan115_root || ''
    if (providerId === 'baidu') return config?.baidu_root || ''
    return routes.find((route) => route.enabled && route.provider_id === providerId && route.local_path)?.local_path || ''
  }
  const providerOption = PROVIDER_OPTIONS.find((option) => option.value === provider) || PROVIDER_OPTIONS[0]
  const sourceCardMetadata = (
    sourceKind: ImportKind,
    selectedProvider: ProviderId,
    selectedSourceRoot: string,
  ): SourceCardMetadata => {
    const route = routeForPath(routes, remoteRoot)
    const providerLabel = selectedProvider === 'other'
      ? '其他远程来源'
      : PROVIDER_OPTIONS.find((option) => option.value === selectedProvider)?.label || '本地媒体'
    if (sourceKind === 'local') {
      const name = finalPathSegment(path) || '本地媒体库'
      return {
        source_display_name: `${name} 媒体库`,
        source_locator: path,
        playback_locator: path,
        source_route_id: '',
      }
    }
    if (sourceKind === 'tree') {
      const name = finalPathSegment(path).replace(/\.[^.]+$/, '') || `${providerLabel} 媒体库`
      return {
        source_display_name: `${providerLabel} · ${name}`,
        source_locator: path,
        playback_locator: selectedSourceRoot,
        source_route_id: '',
      }
    }
    const directory = finalPathSegment(remoteRoot) || '根目录'
    return {
      source_display_name: route?.label || `${providerLabel} · ${directory}`,
      source_locator: remoteRoot,
      playback_locator: selectedSourceRoot,
      source_route_id: route?.route_id || '',
    }
  }

  const handleRemotePathChange = (nextPath: string) => {
    setRemoteRoot(nextPath)
    const matchedRoute = routeForPath(routes, nextPath)
    if (matchedRoute?.provider_id && matchedRoute.provider_id !== 'local' && matchedRoute.provider_id !== 'other') {
      setProvider(matchedRoute.provider_id)
    }
  }

  const clearResultState = () => {
    setScan(null)
    setPreview(null)
    setJobs([])
    setRevisionId('')
    setError('')
    setAllowEmpty(false)
    setOverrideDrafts({})
    setExecuteProgress(null)
    setWorkflowStage('source')
    localStorage.removeItem(ACTIVE_REVISION_KEY)
  }

  const selectSourceKind = (nextKind: ImportKind) => {
    if (nextKind === kind) return
    setKind(nextKind)
    setPath(nextKind === 'local' ? config?.local_root || '' : '')
    setRemoteRoot(config?.openlist_remote_root || '/')
    setProvider('pan115')
    clearResultState()
  }

  const choosePath = async () => {
    if (kind === 'openlist') return
    const selected = kind === 'local'
      ? await pickFolder(path, '选择本地媒体目录')
      : await pickDirectoryTreeFile(path, '选择目录树 TXT')
    if (selected) setPath(selected)
  }

  const scanSource = async (action: 'primary' | 'incremental' | 'full' = 'primary') => {
    const requiresOpenListConfig = kind === 'openlist' || kind === 'hybrid' || action === 'incremental' || action === 'full'
    if (kind !== 'openlist' && action === 'primary' && !path.trim()) {
      setError(kind === 'local' ? '请先选择本地媒体目录' : '请先选择目录树 TXT')
      return
    }
    if (requiresOpenListConfig && !config?.openlist_configured) {
      setError('请先在设置页完成 OpenList 连接配置')
      return
    }
    if (requiresOpenListConfig && !routeForPath(routes, remoteRoot)) {
      setError('请先在 OpenList 浏览器中进入一个已配置内容来源的目录')
      return
    }
    if (action === 'incremental' && openlistBaseline?.has_confirmed_baseline !== true) {
      setError('此 OpenList 目录尚无已确认基线，请先完成并确认首次完整扫描')
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
      const requestSource = action === 'incremental' || action === 'full' ? 'openlist' : kind
      const selectedProvider: ProviderId = requestSource === 'local'
        ? 'local'
        : requestSource === 'openlist' || kind === 'hybrid'
          ? routeForPath(routes, remoteRoot)?.provider_id || 'other'
          : provider
      const selectedSourceRoot = requestSource === 'tree' || requestSource === 'hybrid'
        ? providerRoot(selectedProvider, remoteRoot)
        : requestSource === 'local' ? path : routeForPath(routes, remoteRoot)?.local_path || ''
      const metadata = sourceCardMetadata(kind, selectedProvider, selectedSourceRoot)
      let scanMode: 'auto' | 'full' | 'incremental' = 'auto'
      if (action === 'incremental') scanMode = 'incremental'
      else if (action === 'full') scanMode = 'full'
      else if (requestSource === 'openlist') scanMode = openlistBaseline?.has_confirmed_baseline ? 'incremental' : 'full'
      const result = await mediaV4Api.scan({
        source: requestSource,
        root_path: requestSource === 'local' ? path : requestSource === 'openlist' || kind === 'hybrid' ? remoteRoot : providerRoot(provider) || 'tree',
        tree_file: requestSource === 'tree' || requestSource === 'hybrid' ? path : '',
        provider: selectedProvider,
        source_root: requestSource === 'tree' || requestSource === 'hybrid' ? selectedSourceRoot : '',
        scan_mode: scanMode,
      })
      const nextScan = { ...result, source_metadata: metadata }
      setScan(nextScan)
      setAllowEmpty(false)
      if (result.entries.length > 0) {
        const previewResult = await mediaV4Api.preview({
          revision_id: nextRevisionId,
          root_id: result.root_id,
          scan_id: result.scan_id,
          entries: result.entries,
          allow_empty: false,
          source_mode: result.source_mode || '',
          ...metadata,
        })
        setPreview(previewResult)
        setWorkflowStage('review')
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
        source_mode: scan.source_mode || '',
        ...scan.source_metadata,
      })
      setRevisionId(nextRevisionId)
      setPreview(result)
      setWorkflowStage('review')
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
      // P-003：确认成功后立即切换到独立执行阶段，第二步正文退出。
      const status = await mediaV4Api.status(revisionId)
      if (status.progress) setExecuteProgress(status.progress)
      setWorkflowStage('execute')
      void refreshSourceCards()
      if (kind === 'openlist' || kind === 'hybrid') void refreshOpenlistBaseline(remoteRoot)
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
    setPath(config?.local_root || '')
    setProvider('pan115')
    setRemoteRoot(config?.openlist_remote_root || '/')
    clearResultState()
  }

  const resumeSourceCard = async (card: V4SourceLibraryCard) => {
    setError('')
    try {
      const result = await mediaV4Api.status(card.revision_id)
      setRevisionId(card.revision_id)
      setJobs(result.jobs)
      if (result.progress) setExecuteProgress(result.progress)
      setScan(null)
      setPreview(null)
      localStorage.setItem(ACTIVE_REVISION_KEY, card.revision_id)
      setWorkflowStage('execute')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法读取该媒体库的导入进度')
    }
  }

  const retryJob = async (job: V4Job) => {
    setRetryingJobId(job.job_id)
    setError('')
    try {
      await tasksApi.retry(job.job_id)
      const result = await mediaV4Api.status(job.revision_id)
      setJobs(result.jobs)
      if (result.progress) setExecuteProgress(result.progress)
      void refreshSourceCards()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '任务重试失败')
    } finally {
      setRetryingJobId('')
    }
  }

  const sourceModeLabel = (card: V4SourceLibraryCard) => {
    if (card.source_mode === 'local') return '本地来源'
    if (card.source_mode === 'tree_snapshot') return '目录树基线'
    if (card.source_mode === 'tree_openlist') return '目录树 + OpenList'
    if (card.source_mode === 'openlist_full') return 'OpenList 来源'
    // 兼容回填前的旧卡：按遗留 ingest_method 展示，不作为新判断依据。
    return card.ingest_method === 'local_scan' ? '本地来源' : card.ingest_method === 'directory_tree' ? '目录树基线' : 'OpenList 来源'
  }

  const prepareSourceUpdate = (card: V4SourceLibraryCard) => {
    clearResultState()
    setBrowserSession((current) => current + 1)
    const nextProvider = card.provider === 'baidu' || card.provider === 'quark' ? card.provider : 'pan115'
    setProvider(nextProvider)
    if (card.provider === 'local') {
      setKind('local')
      setPath(card.source_locator || card.playback_locator)
      return
    }
    if (card.source_mode === 'tree_snapshot' || (card.source_mode === '' && !card.route_id)) {
      setKind('tree')
      setPath(card.source_locator)
      return
    }
    if (card.source_mode === 'tree_openlist' || (card.source_mode === '' && card.route_id)) {
      setKind('hybrid')
      setPath('')
      setRemoteRoot(card.source_locator || config?.openlist_remote_root || '/')
      return
    }
    // openlist_full（或旧卡的 openlist_api）：回到同一 OpenList 远端根。
    setKind('openlist')
    setPath('')
    setRemoteRoot(card.source_locator || config?.openlist_remote_root || '/')
  }

  return (
    <div className="media-flow-page media-v4-page">
      <header className="media-flow-header">
        <div className="media-flow-title">
          <span>媒体管理</span>
          <h1>导入媒体</h1>
          <p>选择一个媒体来源，检查识别结果，然后建立可播放的媒体库。</p>
        </div>
        {showReset && <Button className="media-v4-new-import" appearance="subtle" icon={<ArrowReset24Regular />} onClick={startNewImport}>重新开始</Button>}
      </header>

      {(sourceCardsLoading || sourceCards.length > 0) && <section className="media-v4-source-libraries" aria-label="已导入媒体库">
        <div className="media-v4-source-libraries-heading">
          <div><span>已导入媒体库</span><h2>来源卡</h2><p>每张卡代表一个已确认的媒体来源，可随时回到该次导入的真实任务进度。</p></div>
          <Button appearance="subtle" icon={<ArrowSync24Regular />} disabled={sourceCardsLoading} onClick={() => void refreshSourceCards()}>刷新状态</Button>
        </div>
        {sourceCardsLoading && sourceCards.length === 0 ? <div className="media-v4-source-card-loading"><Spinner size="small" />正在读取媒体库…</div> : <div className="media-v4-source-library-grid">
          {sourceCards.map((card) => {
            const pending = card.job_summary.queued + card.job_summary.running
            const active = pending > 0
            const progress = card.job_summary.total === 0 ? 100 : Math.round(((card.job_summary.succeeded + card.job_summary.failed + card.job_summary.cancelled) / card.job_summary.total) * 100)
            const progressLabel = pending > 0
              ? '正在处理'
              : card.job_summary.failed > 0 ? '有失败任务' : card.job_summary.cancelled > 0 ? '有已取消任务' : '上次导入已处理完毕'
            return <article className={`media-v4-library-source-card ${card.can_resume ? 'active' : 'settled'}`} key={card.root_id}>
              <div className="media-v4-library-source-card-top"><span className="media-v4-provider-mark" aria-hidden="true">{card.provider === 'pan115' ? '115' : card.provider === 'baidu' ? '百' : card.provider === 'quark' ? '夸' : card.provider === 'local' ? '本' : '远'}</span><span>{sourceModeLabel(card)}</span></div>
              <strong title={card.display_name}>{card.display_name}</strong>
              <span className="media-v4-source-card-locator" title={card.source_locator || card.playback_locator}>{card.source_locator || card.playback_locator || '已确认的媒体来源'}</span>
              <div className="media-v4-source-card-stats"><span>{card.work_count} 部作品</span><span>{card.asset_count} 个文件</span><span>{card.evidence_count} 条来源证据</span></div>
              <div className="media-v4-source-card-progress"><div><span>{card.can_resume ? `${progressLabel} · ${progress}%` : progressLabel}</span><span>{card.job_summary.total} 个任务</span></div><i aria-hidden="true"><b style={{ width: `${progress}%` }} /></i></div>
              <div className="media-v4-source-card-actions">
                <Button appearance={card.can_resume ? 'primary' : 'secondary'} onClick={() => void resumeSourceCard(card)}>{card.can_resume ? '查看进度' : '查看上次导入'}</Button>
                <Button appearance={card.can_resume ? 'secondary' : 'primary'} icon={<ArrowSync24Regular />} disabled={active} onClick={() => prepareSourceUpdate(card)}>检查更新</Button>
              </div>
            </article>
          })}
        </div>}
      </section>}

      <nav className="media-v4-steps" aria-label="导入步骤">
        <ol>
          {IMPORT_STEPS.map((step, index) => {
            const StepIcon = step.icon
            return (
              <li className={index < activeStep ? 'complete' : index === activeStep ? 'active' : ''} key={step.label} aria-current={index === activeStep ? 'step' : undefined}>
                <span className="media-v4-step-index" aria-hidden="true">{index < activeStep ? <CheckmarkCircle24Filled /> : <StepIcon />}</span>
                <span>{step.label}</span>
              </li>
            )
          })}
        </ol>
      </nav>

      {error && <MessageBar className="media-v4-message" intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      {workflowStage === 'source' && <section className="media-stage-shell media-v4-stage-panel media-v4-source-card">
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

        <div className={`media-v4-config-panel media-v4-workspace workspace-${kind}`}>
          <div className="media-v4-config-heading">
            <strong>{kind === 'local' ? '选择本机文件夹' : kind === 'tree' ? '导入目录树清单' : kind === 'hybrid' ? '建立基线并检查后续变化' : '浏览 OpenList 目录'}</strong>
            <span>{kind === 'local'
              ? '只扫描本机物理磁盘。网盘挂载请使用目录树或 OpenList。'
              : kind === 'tree'
                ? '选择内容来源和 TXT 文件；播放路径自动使用设置中的挂载映射。'
                : kind === 'hybrid'
                  ? '先用 TXT 快速建立大库基线，确认后再通过 OpenList 对同一目录执行增量检查。'
                  : '像文件管理器一样进入目标文件夹，然后完整扫描当前目录。'}</span>
          </div>

          {kind === 'local' && (
            <div className="media-v4-workspace-body">
              <div className="media-v4-field-block">
                <div className="media-v4-field-copy"><strong>本机媒体文件夹</strong><span>默认读取“设置 → 媒体来源”中的本地媒体根路径。</span></div>
                <div className="media-v4-path-row media-v4-path-row-wide">
                  <Input aria-label="本机媒体文件夹" name="media_path" autoComplete="off" spellCheck={false} value={path} onChange={(_, data) => setPath(data.value)} placeholder="例如 D:\\动画" />
                  <Button appearance="secondary" icon={<FolderOpen24Regular />} onClick={() => void choosePath()}>选择文件夹</Button>
                </div>
              </div>
              <div className="media-v4-command-row">
                <div><strong>{path.trim() ? '已选择本机文件夹' : '尚未选择文件夹'}</strong><span>{path.trim() ? '扫描只读取媒体文件，不移动或改名原文件。' : '选择本机物理磁盘中的媒体文件夹后继续。'}</span></div>
                <Button aria-label="扫描并识别" className="media-primary-command" appearance="primary" icon={<ScanObject24Regular />} disabled={busy !== '' || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : '扫描并识别'}</Button>
              </div>
            </div>
          )}

          {kind === 'tree' && (
            <div className="media-v4-workspace-body">
              <div className="media-v4-field-block">
                <div className="media-v4-field-copy"><strong>内容来源</strong><span>选择 TXT 清单中的媒体实际属于哪个网盘。</span></div>
                <ProviderPicker value={provider} onChange={setProvider} />
                <a className="media-v4-provider-link" href={providerOption.website} target="_blank" rel="noreferrer">{providerOption.websiteLabel}<span aria-hidden="true">↗</span></a>
              </div>
              <div className="media-v4-field-block">
                <div className="media-v4-field-copy"><strong>目录树 TXT 文件</strong><span>支持 115、百度、夸克和 OpenList 导出的目录清单，文件可位于网盘挂载盘。</span></div>
                <div className="media-v4-path-row media-v4-path-row-wide">
                  <Input aria-label="目录树 TXT 文件" name="tree_file" autoComplete="off" spellCheck={false} value={path} onChange={(_, data) => setPath(data.value)} placeholder="例如 K:\\媒体清单\\动画目录树.txt" />
                  <Button appearance="secondary" icon={<DocumentText24Regular />} onClick={() => void choosePath()}>选择文件</Button>
                </div>
              </div>
              <div className="media-v4-mapping-note">
                <Database24Regular aria-hidden="true" />
                <div><strong>播放路径由设置自动匹配</strong><span>{providerRoot(provider) ? <>播放路径将使用设置中的 <code title={providerRoot(provider)}>{providerRoot(provider)}</code></> : '当前来源尚未配置可用挂载路径。'}</span></div>
                {!providerRoot(provider) && <Button appearance="subtle" onClick={goSettings}>前往设置</Button>}
              </div>
              <div className="media-v4-command-row">
                <div><strong>{path.trim() ? '目录树已就绪' : '尚未选择 TXT 文件'}</strong><span>{path.trim() ? `将按${providerOption.label}来源生成识别结果。` : '选择目录树文件后即可扫描。'}</span></div>
                <Button aria-label="扫描并识别" className="media-primary-command" appearance="primary" icon={<ScanObject24Regular />} disabled={busy !== '' || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : '扫描并识别'}</Button>
              </div>
            </div>
          )}

          {kind === 'openlist' && (
            <div className="media-v4-workspace-body">
              <OpenListFolderBrowser key={`openlist-${browserSession}`} configured={Boolean(config?.openlist_configured)} initialPath={remoteRoot || config?.openlist_remote_root || '/'} onLoadingChange={setRemoteBrowsing} onPathChange={handleRemotePathChange} onGoSettings={goSettings} />
              <div className="media-v4-mapping-note">
                <Cloud24Regular aria-hidden="true" />
                <div><strong>{selectedRemoteRoute ? selectedRemoteRoute.label : '当前目录尚未匹配内容路由'}</strong><span>{selectedRemoteRoute ? `内容来源：${PROVIDER_OPTIONS.find((item) => item.value === selectedRemoteRoute.provider_id)?.label || '其他远程来源'}；播放位置由已保存路由推导。` : '请先进入一个已配置内容来源的目录，才能开始扫描。'}</span></div>
                {!selectedRemoteRoute && <Button appearance="subtle" onClick={goSettings}>配置来源路由</Button>}
              </div>
              <div className="media-v4-command-row">
                {openlistBaseline?.has_confirmed_baseline ? (
                  <>
                    <div><strong>已有已确认基线，默认增量更新</strong><span>增量只核对新增和变化目录；远端异常时可执行完整校验。</span></div>
                    <div className="media-v4-command-buttons">
                      <Button appearance="secondary" icon={<ScanObject24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource('full')}>{busy === 'scan' ? <Spinner size="tiny" /> : remoteBrowsing ? '正在切换目录' : '完整校验'}</Button>
                      <Button aria-label="增量扫描" className="media-primary-command" appearance="primary" icon={<ArrowSync24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource('incremental')}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : remoteBrowsing ? '正在切换目录' : '增量扫描'}</Button>
                    </div>
                  </>
                ) : (
                  <>
                    <div><strong>首次完整扫描建立基线</strong><span>确认本次导入后，此目录将解锁增量更新；当前还不能增量扫描。</span></div>
                    <div className="media-v4-command-buttons">
                      <Button appearance="secondary" icon={<ArrowSync24Regular />} disabled>增量扫描</Button>
                      <Button aria-label="完整扫描并建立基线" className="media-primary-command" appearance="primary" icon={<ScanObject24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : remoteBrowsing ? '正在切换目录' : '完整扫描并建立基线'}</Button>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}

          {kind === 'hybrid' && (
            <div className="media-v4-workspace-body">
              <div className="media-v4-hybrid-grid">
                <div className="media-v4-field-block">
                  <div className="media-v4-field-copy"><span className="media-v4-action-index">首次</span><strong>选择 TXT 基线</strong><span>目录树负责快速建立大库的完整基线。</span></div>
                  <div className="media-v4-path-row media-v4-path-row-wide">
                    <Input aria-label="首次目录树 TXT 文件" name="hybrid_tree_file" autoComplete="off" spellCheck={false} value={path} onChange={(_, data) => setPath(data.value)} placeholder="例如 K:\\媒体清单\\动画目录树.txt" />
                    <Button appearance="secondary" icon={<DocumentText24Regular />} onClick={() => void choosePath()}>选择文件</Button>
                  </div>
                </div>
                <div className="media-v4-field-block">
                  <div className="media-v4-field-copy"><span className="media-v4-action-index">后续</span><strong>选择同一 OpenList 目录</strong><span>确认 TXT 基线后，增量只核对新增和变化目录。</span></div>
                  <OpenListFolderBrowser key={`hybrid-${browserSession}`} configured={Boolean(config?.openlist_configured)} initialPath={remoteRoot || config?.openlist_remote_root || '/'} onLoadingChange={setRemoteBrowsing} onPathChange={handleRemotePathChange} onGoSettings={goSettings} />
                </div>
              </div>
              <div className="media-v4-mapping-note">
                <Cloud24Regular aria-hidden="true" />
                <div><strong>{selectedRemoteRoute ? selectedRemoteRoute.label : '当前目录尚未匹配内容路由'}</strong><span>{selectedRemoteRoute ? 'TXT 的内容来源与播放位置将使用这条已保存路由，不需要重复选择。' : '请先进入一个已配置内容来源的目录。'}</span></div>
                {!selectedRemoteRoute && <Button appearance="subtle" onClick={goSettings}>配置来源路由</Button>}
              </div>
              <div className="media-v4-command-row media-v4-hybrid-actions">
                <div><strong>{openlistBaseline?.has_confirmed_baseline ? '基线已确认，日常使用增量扫描' : '两个动作互不混淆'}</strong><span>{openlistBaseline?.has_confirmed_baseline ? '增量只核对新增和变化目录；需要时可重新建立 TXT 基线或执行完整校验。' : '第一次建立并确认基线；以后从同一来源卡进入时执行增量扫描。'}</span></div>
                <div className="media-v4-command-buttons">
                  <Button aria-label="建立 TXT 基线" appearance={openlistBaseline?.has_confirmed_baseline ? 'secondary' : 'primary'} icon={<DocumentText24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <Spinner size="tiny" /> : remoteBrowsing ? '正在切换目录' : '建立 TXT 基线'}</Button>
                  <Button aria-label="增量扫描" className={openlistBaseline?.has_confirmed_baseline ? 'media-primary-command' : ''} appearance={openlistBaseline?.has_confirmed_baseline ? 'primary' : 'secondary'} icon={<ArrowSync24Regular />} disabled={busy !== '' || remoteBrowsing || !config?.openlist_configured || !remoteRoot || !selectedRemoteRoute?.local_path || openlistBaseline?.has_confirmed_baseline !== true} onClick={() => void scanSource('incremental')}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : remoteBrowsing ? '正在切换目录' : '增量扫描'}</Button>
                </div>
              </div>
            </div>
          )}
        </div>
      </section>}

      {workflowStage === 'review' && scan && <section className="media-stage-shell media-v4-stage-panel media-v4-review-card">
        <div className="media-stage-header">
          <div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><CheckmarkCircle24Regular /></span><div><span className="media-stage-eyebrow">第 2 步</span><h2>检查识别结果</h2><p>已扫描 {scan.entries.length} 个媒体条目。默认按作品摘要检查，需要处理的条目会置顶。</p></div></div>
          {!preview && <Button appearance="secondary" disabled={busy !== '' || (scan.entries.length === 0 && !allowEmpty)} onClick={() => void buildPreview()}>{busy === 'preview' ? <Spinner size="tiny" /> : '生成识别预览'}</Button>}
        </div>
        {scan.scan_mode === 'incremental' && <MessageBar intent="info"><MessageBarBody>本次使用 OpenList 增量核对：请求 {scan.scan_stats?.requested_directories || 0} 个目录，其中滚动抽查 {scan.scan_stats?.rolling_verified || 0} 个、变化优先核对 {scan.scan_stats?.changed_directories || 0} 个。</MessageBarBody></MessageBar>}
        {scan.scan_mode === 'tree_baseline' && <MessageBar intent="info"><MessageBarBody>TXT 基线已建立。确认本次导入后，再扫描同一 OpenList 目录时会自动进入风险受控增量核对。</MessageBarBody></MessageBar>}
        {!preview && <div className="media-v4-empty">{scan.entries.length === 0 ? <Checkbox checked={allowEmpty} onChange={(_, data) => setAllowEmpty(Boolean(data.checked))} label="我确认该来源当前确实为空，并允许移除它先前导入的媒体" /> : '正在生成识别结果…'}</div>}
        {preview && <>
          {preview.issues.length > 0 && <MessageBar intent="warning"><MessageBarBody>发现 {preview.issues.length} 个需要人工处理的问题；未解决前不能确认。</MessageBarBody></MessageBar>}
          <V4RecognitionSummary
            preview={preview}
            issues={preview.issues}
            overrideDrafts={overrideDrafts}
            busy={busy !== ''}
            onOverrideChange={(evidenceId, draft) => setOverrideDrafts((current) => ({ ...current, [evidenceId]: draft }))}
            onApplyOverride={(evidenceId) => void applyOverride(evidenceId)}
          />
          <div className="media-v4-command-row media-v4-confirm-row"><div><strong>{preview.issues.length > 0 ? '需要先处理识别问题' : '识别结果可以建立媒体库'}</strong><span>确认后将生成镜像、获取媒体信息并更新媒体库。</span></div><Button className="media-primary-command" appearance="primary" icon={<Database24Regular />} disabled={busy !== '' || preview.issues.length > 0 || preview.status === 'confirmed'} onClick={() => void confirmRevision()}>{busy === 'confirm' ? <><Spinner size="tiny" />正在建立</> : preview.status === 'confirmed' ? '已建立媒体库' : '确认并建立媒体库'}</Button></div>
        </>}
      </section>}

      {workflowStage === 'execute' && <section className="media-stage-shell media-v4-stage-panel media-v4-jobs-card">
        <div className="media-stage-header">
          <div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><Database24Regular /></span><div><span className="media-stage-eyebrow">第 3 步</span><h2>建立媒体库</h2><p>可以离开此页面；返回后会继续显示当前导入进度。</p></div></div>
          {preview && <details className="media-v4-review-details"><summary>查看本次识别摘要</summary><div className="media-v4-review-details-body">
            <V4RecognitionSummary
              preview={preview}
              issues={preview.issues}
              overrideDrafts={overrideDrafts}
              busy={busy !== ''}
              onOverrideChange={(evidenceId, draft) => setOverrideDrafts((current) => ({ ...current, [evidenceId]: draft }))}
              onApplyOverride={(evidenceId) => void applyOverride(evidenceId)}
            />
          </div></details>}
        </div>
        {executeProgress ? (
          <V4ExecutionProgressView
            progress={executeProgress}
            busyRetryId={retryingJobId}
            onRetry={(jobId: string) => { const job = jobs.find((item) => item.job_id === jobId); if (job) void retryJob(job) }}
          />
        ) : (
          <div className="media-v4-empty">正在读取执行进度…</div>
        )}
      </section>}

    </div>
  )
}