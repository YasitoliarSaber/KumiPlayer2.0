import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, FluentProvider, Input, MessageBar, MessageBarBody, ProgressBar, Select, Spinner } from '@fluentui/react-components'
import {
  Add24Regular,
  ArrowLeft24Regular,
  ArrowReset24Regular,
  ArrowSync24Regular,
  CheckmarkCircle24Filled,
  CheckmarkCircle24Regular,
  Cloud24Regular,
  Delete24Regular,
  Dismiss24Regular,
  ShieldCheckmark24Regular,
  Database24Regular,
  DocumentText24Regular,
  Edit24Regular,
  Folder24Regular,
  FolderOpen24Regular,
  ScanObject24Regular,
} from '@fluentui/react-icons'
import { mediaV4Api, type V4Job, type V4OpenlistBaselineStatus, type V4Preview, type V4SourceEvidence, type V4SourceLibraryCard } from '../api/mediaV4'
import type { V4ExecutionProgress } from '../api/mediaV4'
import { ApiError } from '../api/client'
import { configApi, type PublicConfig } from '../api/config'
import { openlistApi } from '../api/openlist'
import { tasksApi } from '../api/tasks'
import type { OpenListRoute, ProviderId } from '../api/types'
import { MediaProviderIcon, providerVisualFor } from '../components/media/MediaProviderIcon'
import OpenListFolderBrowser from '../components/media/OpenListFolderBrowser'
import { V4ExecutionProgress as V4ExecutionProgressView } from '../components/media/V4ExecutionProgress'
import { V4RecognitionSummary, type OverrideDraft } from '../components/media/V4RecognitionSummary'
import { LibraryMaintenancePanel } from '../components/media/LibraryMaintenancePanel'
import { pickDirectoryTreeFile, pickFolder } from '../platform/folderPicker'
import { useMediaWorkflowStore } from '../stores/mediaWorkflow'
import { useUiStore } from '../stores/ui'
import { getKumiFluentTheme } from '../design/fluentTheme'

type ImportKind = 'local' | 'tree' | 'openlist' | 'hybrid'
type WorkflowStage = 'source' | 'review' | 'execute'
type SourceCardMetadata = {
  source_display_name: string
  source_locator: string
  playback_locator: string
  source_route_id: string
}

type MetadataRecoveryCandidate = {
  candidate_id: string
  provider_id: string
  media_type: string
  title: string
  original_title: string
  year: number | null
  aliases: string[]
  score?: number
  reasons?: string[]
  recommended?: boolean
}

type DurableScanState = {
  scan_id: string
  root_id: string
  status: string
  interrupted?: boolean
  error?: string
  evidence_count?: number
  stage?: string
  stage_label?: string
  processed_count?: number
  total_count?: number
  progress?: number | null
  heartbeat_at?: string
  cancel_requested?: boolean
  entries?: V4SourceEvidence[]
}

type DurableScanTask = Pick<DurableScanState, 'scan_id' | 'status' | 'stage' | 'stage_label' | 'processed_count' | 'total_count' | 'progress' | 'heartbeat_at' | 'cancel_requested'>

const ACTIVE_REVISION_KEY = 'kumiplayer.media-v4.active-revision'
const TRANSIENT_BACKEND_RETRY_DELAY_MS = 300

function isTransientBackendFailure(cause: unknown) {
  return cause instanceof ApiError && (cause.status === 408 || cause.status === 503)
}

function isTransientBackendMessage(message: string) {
  return message.startsWith('无法连接 KumiPlayer 后端') || message.startsWith('请求超时')
}

function userFacingPageError(cause: unknown, fallback: string) {
  const message = typeof cause === 'object' && cause !== null && 'message' in cause && typeof cause.message === 'string'
    ? cause.message.trim()
    : ''
  if (!message || message.length > 240 || !/[\u4e00-\u9fff]/u.test(message)) return fallback
  if (/sqlite|sql|traceback|exception|constraint|requests|httpx|aiohttp|connectionerror|operationalerror|integrityerror|enoent|eacces|timeouterror|root_[A-Za-z0-9_-]+|revision_id|job_id|provider_bindings|database/iu.test(message)) {
    return fallback
  }
  return message
}

const SOURCE_OPTIONS: Array<{
  kind: ImportKind
  label: string
  icon: typeof Folder24Regular
}> = [
  { kind: 'local', label: '本地目录', icon: Folder24Regular },
  { kind: 'tree', label: '目录树 TXT', icon: DocumentText24Regular },
  { kind: 'openlist', label: 'OpenList', icon: Cloud24Regular },
  { kind: 'hybrid', label: '目录树 + OpenList 增量', icon: ArrowSync24Regular },
]

const PROVIDER_OPTIONS: Array<{
  value: Exclude<ProviderId, 'local' | 'other'>
  label: string
  website: string
  websiteLabel: string
}> = [
  { value: 'pan115', label: '115 网盘', website: 'https://115.com/', websiteLabel: '前往 115 官网生成目录树' },
  { value: 'baidu', label: '百度网盘', website: 'https://pan.baidu.com/', websiteLabel: '前往百度网盘官网' },
  { value: 'quark', label: '夸克网盘', website: 'https://pan.quark.cn/', websiteLabel: '前往夸克网盘官网' },
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

function normalizeRemotePath(path: string) {
  const normalized = (path || '/').replace(/\\/g, '/').replace(/\/+/g, '/')
  return normalized === '/' ? '/' : `/${normalized.replace(/^\/+|\/+$/g, '')}`
}

function pathSegments(path: string) {
  return normalizeRemotePath(path).split('/').filter(Boolean)
}

function routeForPath(routes: OpenListRoute[], remotePath: string) {
  const normalized = normalizeRemotePath(remotePath)
  return routes
    .filter((route) => {
      if (!route.enabled) return false
      const prefix = normalizeRemotePath(route.remote_prefix)
      return prefix === '/' || normalized === prefix || normalized.startsWith(`${prefix}/`)
    })
    .sort((left, right) => pathSegments(right.remote_prefix).length - pathSegments(left.remote_prefix).length)[0]
}

function playbackRootForRoute(route: OpenListRoute | undefined, remotePath: string) {
  if (!route?.local_path) return ''
  const remoteParts = pathSegments(remotePath)
  const routeParts = pathSegments(route.remote_prefix)
  // 内容路由是明确的 remote_prefix ↔ local_path 映射：local_path 已经是
  // remote_prefix 的本地落点。扫描的子目录只追加该前缀之后的部分，不能
  // 把 `/115` 等远端路由名又拼进 `K:\115网盘`。
  const routeMatches = routeParts.length === 0 || routeParts.every(
    (part, index) => remoteParts[index]?.toLocaleLowerCase() === part.toLocaleLowerCase(),
  )
  const relativeParts = routeMatches ? remoteParts.slice(routeParts.length) : remoteParts
  if (relativeParts.length === 0) return route.local_path
  const separator = route.local_path.includes('\\') ? '\\' : '/'
  return `${route.local_path.replace(/[\\/]+$/, '')}${separator}${relativeParts.join(separator)}`
}

function scanTaskFromState(state: DurableScanState): DurableScanTask {
  return {
    scan_id: state.scan_id,
    status: state.status,
    stage: state.stage,
    stage_label: state.stage_label,
    processed_count: state.processed_count,
    total_count: state.total_count,
    progress: state.progress,
    heartbeat_at: state.heartbeat_at,
    cancel_requested: state.cancel_requested,
  }
}

function isActiveScanStatus(status: string | undefined) {
  return status === 'queued' || status === 'running' || status === 'cancelling'
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
          <span className={`media-v4-provider-mark provider-${option.value}`} aria-hidden="true"><MediaProviderIcon provider={option.value} /></span>
          <span><strong>{option.label}</strong></span>
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
  const pageMode = useUiStore((state) => state.manageView)
  const goManageView = useUiStore((state) => state.goManageView)
  const appearanceMode = useUiStore((state) => state.appearanceMode)
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
  const [sourceCardPendingDelete, setSourceCardPendingDelete] = useState<V4SourceLibraryCard | null>(null)
  const [sourceCardDeleting, setSourceCardDeleting] = useState(false)
  const [sourceCardPendingRename, setSourceCardPendingRename] = useState<V4SourceLibraryCard | null>(null)
  const [sourceCardRenameValue, setSourceCardRenameValue] = useState('')
  const [sourceCardRenaming, setSourceCardRenaming] = useState(false)
  const [sourceCardRenameError, setSourceCardRenameError] = useState('')
  const [revisionId, setRevisionId] = useState('')
  const [workflowStage, setWorkflowStage] = useState<WorkflowStage>('source')
  const [executeProgress, setExecuteProgress] = useState<V4ExecutionProgress | null>(null)
  const [scan, setScan] = useState<{
    root_id: string
    scan_id: string
    entries: V4SourceEvidence[]
    evidence_count: number
    scan_mode?: 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
    source_mode?: string
    scan_stats?: { requested_directories?: number; rolling_verified?: number; changed_directories?: number }
    source_metadata: SourceCardMetadata
  } | null>(null)
  const [preview, setPreview] = useState<V4Preview | null>(null)
  const [jobs, setJobs] = useState<V4Job[]>([])
  const [busy, setBusy] = useState<'scan' | 'preview' | 'override' | 'confirm' | ''>('')
  const [retryingJobId, setRetryingJobId] = useState('')
  const [metadataRecovery, setMetadataRecovery] = useState<{ workId: string; workTitle: string; candidates: MetadataRecoveryCandidate[] } | null>(null)
  const [metadataRecoveryQuery, setMetadataRecoveryQuery] = useState('')
  const [metadataRecoveryBusy, setMetadataRecoveryBusy] = useState('')
  const [scanTask, setScanTask] = useState<DurableScanTask | null>(null)
  const [error, setError] = useState('')
  const [allowEmpty, setAllowEmpty] = useState(false)
  const [overrideDrafts, setOverrideDrafts] = useState<Record<string, OverrideDraft>>({})
  const sourceCardsRefreshInFlight = useRef(false)
  // 每次开始、取消或切换来源都会递增；旧请求即使晚返回，也不得覆盖新流程。
  const scanRunRef = useRef(0)

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
      if (alive) setError(userFacingPageError(cause, '无法读取媒体来源设置'))
    })
    return () => { alive = false }
  }, [])

  const refreshSourceCards = useCallback(async () => {
    if (sourceCardsRefreshInFlight.current) return
    sourceCardsRefreshInFlight.current = true
    setSourceCardsLoading(true)
    try {
      const loadCards = () => mediaV4Api.sourceLibraries()
      let result: Awaited<ReturnType<typeof loadCards>>
      try {
        result = await loadCards()
      } catch (cause) {
        // 桌面壳刚拉起后端时，首个请求可能早于监听端口就绪。只对明确的
        // 可恢复连接错误重试一次，避免把服务端校验/业务错误静默吞掉。
        if (!isTransientBackendFailure(cause)) throw cause
        await new Promise<void>((resolve) => window.setTimeout(resolve, TRANSIENT_BACKEND_RETRY_DELAY_MS))
        result = await loadCards()
      }
      setSourceCards(result.cards)
      setError((current) => isTransientBackendMessage(current) ? '' : current)
    } catch (cause) {
      setError(userFacingPageError(cause, '无法读取已导入媒体库'))
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

  // `active_task` 是来源卡唯一的当前任务投影。job_summary 是诊断/历史汇总，
  // 在任务收口和卡片刷新之间可能短暂滞后，不能再反向驱动轮询或禁用操作。
  const hasActiveSourceJobs = sourceCards.some((card) => Boolean(card.active_task))
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
      // P-005 返工 11.13-1：默认停留来源卡 overview，后台状态只更新卡片；
      // 用户显式点击“查看进度/上次导入”后才进入 execute。
      setWorkflowStage('execute')
    }).catch(() => {
      localStorage.removeItem(ACTIVE_REVISION_KEY)
    })
  }, [])

  const hasActiveJobs = jobs.some((job) => !['succeeded', 'failed', 'cancelled'].includes(job.status))
  const executionActive = executeProgress?.overall_status === 'running' || executeProgress?.overall_status === 'queued'
  const executionMirrorTotal = executeProgress?.stage_summary.mirror.total ?? 0
  const executionMirrorDone = executeProgress
    ? executeProgress.stage_summary.mirror.succeeded
      + executeProgress.stage_summary.mirror.failed
      + executeProgress.stage_summary.mirror.cancelled
    : 0
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
        if (!cancelled) setError(userFacingPageError(cause, '后台任务状态读取失败'))
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
        if (!cancelled) setError(userFacingPageError(cause, '后台任务状态读取失败'))
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
  const showReset = workflowStage !== 'source' || kind !== 'local' || Boolean(error) || busy === 'scan' || Boolean(scanTask)

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
    selectedSourceRoot: string,
  ): SourceCardMetadata => {
    const route = routeForPath(routes, remoteRoot)
    if (sourceKind === 'local') {
      const name = finalPathSegment(path) || '本地媒体库'
      return {
        source_display_name: name,
        source_locator: path,
        playback_locator: path,
        source_route_id: '',
      }
    }
    if (sourceKind === 'tree') {
      const name = finalPathSegment(selectedSourceRoot)
        || finalPathSegment(path).replace(/\.[^.]+$/, '')
        || '目录树导入'
      return {
        source_display_name: name,
        source_locator: path,
        playback_locator: selectedSourceRoot,
        source_route_id: '',
      }
    }
    const directory = finalPathSegment(remoteRoot) || '根目录'
    return {
      source_display_name: directory,
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

  const abandonActiveScan = () => {
    scanRunRef.current += 1
    const activeScanId = scanTask?.scan_id
    setScanTask(null)
    setBusy('')
    if (activeScanId) {
      void mediaV4Api.cancelDurableScan(activeScanId).catch(() => undefined)
      void refreshSourceCards()
    }
  }

  const selectSourceKind = (nextKind: ImportKind) => {
    if (nextKind === kind) return
    abandonActiveScan()
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
    const runId = ++scanRunRef.current
    const isCurrentRun = () => scanRunRef.current === runId
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
      const metadata = sourceCardMetadata(kind, selectedSourceRoot)
      let scanMode: 'auto' | 'full' | 'incremental' = 'auto'
      if (action === 'incremental') scanMode = 'incremental'
      else if (action === 'full') scanMode = 'full'
      else if (requestSource === 'openlist') scanMode = openlistBaseline?.has_confirmed_baseline ? 'incremental' : 'full'
      // 所有来源都先创建 durable SourceScan；大目录与目录树读取不能占用 HTTP 请求。
      let result: {
        root_id: string
        scan_id: string
        entries: V4SourceEvidence[]
        evidence_count: number
        scan_mode?: 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
        source_mode?: string
        scan_stats?: { requested_directories?: number; rolling_verified?: number; changed_directories?: number }
      }
      {
        const task = await mediaV4Api.startDurableScan({
          source: requestSource,
          root_path: requestSource === 'local' ? path : kind === 'hybrid' ? remoteRoot : requestSource === 'openlist' ? remoteRoot : providerRoot(selectedProvider, remoteRoot) || 'tree',
          tree_file: requestSource === 'tree' || kind === 'hybrid' ? path : '',
          provider: selectedProvider,
          source_root: selectedSourceRoot,
          scan_mode: scanMode === 'incremental' ? 'incremental' : 'full',
          revision_id: nextRevisionId,
          source_display_name: metadata.source_display_name,
        })
        // 用户可能在创建请求返回前点击“重新开始”；此时要取消迟到的任务，
        // 不能让旧扫描在后台无主运行，也不能把它的结果写回新流程。
        if (!isCurrentRun()) {
          await mediaV4Api.cancelDurableScan(task.scan_id).catch(() => undefined)
          return
        }
        setScanTask({
          scan_id: task.scan_id,
          status: task.status || 'running',
          stage: task.status === 'queued' ? 'queued' : 'reading_source',
          stage_label: task.status === 'queued' ? '准备读取媒体来源' : '正在读取媒体来源',
          processed_count: 0,
          total_count: 0,
          progress: null,
          heartbeat_at: '',
        })
        // 来源卡在扫描开始时就出现；用户离开导入页后仍可从卡片恢复。
        void refreshSourceCards()
        while (true) {
          await new Promise((resolve) => window.setTimeout(resolve, 900))
          if (!isCurrentRun()) return
          const state = await mediaV4Api.durableScan(task.scan_id) as DurableScanState
          if (!isCurrentRun()) return
          setScanTask(scanTaskFromState(state))
          if (state.status === 'completed') {
            result = { root_id: state.root_id || task.root_id, scan_id: task.scan_id, entries: [], evidence_count: state.evidence_count ?? (state.entries || []).length, scan_mode: task.scan_mode as 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full', source_mode: task.source_mode || (task.scan_mode === 'incremental' ? openlistBaseline?.source_mode || 'openlist_full' : 'openlist_full') }
            break
          }
          if (state.interrupted) {
            // 执行进程已失联：任务即将被恢复器收口，页面必须显示中断
            // 而不是无限转圈；文案直接采用后端的人类可读阶段描述。
            throw new Error(state.error || state.stage_label || '上次扫描意外中断，请重新扫描')
          }
          if (state.status === 'failed' || state.status === 'cancelled') {
            throw new Error(state.error || (state.status === 'cancelled' ? '扫描已取消' : '来源扫描失败'))
          }
        }
      }
      if (!isCurrentRun()) return
      const nextScan = { ...result, source_metadata: metadata }
      setScan(nextScan)
      setAllowEmpty(false)
      if (result.evidence_count > 0) {
        const previewResult = await mediaV4Api.preview({
          revision_id: nextRevisionId,
          root_id: result.root_id,
          scan_id: result.scan_id,
          entries: [],
          allow_empty: false,
          source_mode: result.source_mode || '',
          ...metadata,
        })
        if (!isCurrentRun()) return
        setPreview(previewResult)
      }
      if (!isCurrentRun()) return
      setScanTask(null)
      setWorkflowStage('review')
      goManageView('import')
    } catch (cause) {
      if (isCurrentRun()) setError(userFacingPageError(cause, '来源扫描失败'))
    } finally {
      if (isCurrentRun()) {
        setScanTask(null)
        setBusy('')
      }
    }
  }

  const cancelScanTask = async () => {
    if (!scanTask) return
    const currentScanId = scanTask.scan_id
    setScanTask((current) => current?.scan_id === currentScanId ? { ...current, status: 'cancelling', cancel_requested: true, stage_label: '正在取消扫描' } : current)
    try {
      await mediaV4Api.cancelDurableScan(currentScanId)
    } catch {
      // 取消请求失败不阻塞；轮询会看到终态。
    }
  }

  const buildPreview = async () => {
    if (!scan) return
    if (scan.evidence_count === 0 && !allowEmpty) {
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
        entries: [],
        allow_empty: allowEmpty,
        source_mode: scan.source_mode || '',
        ...scan.source_metadata,
      })
      setRevisionId(nextRevisionId)
      setPreview(result)
      setWorkflowStage('review')
      goManageView('import')
    } catch (cause) {
      setError(userFacingPageError(cause, '识别预览失败'))
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
      goManageView('import')
      void refreshSourceCards()
      if (kind === 'openlist' || kind === 'hybrid') void refreshOpenlistBaseline(remoteRoot)
    } catch (cause) {
      setError(userFacingPageError(cause, '确认导入失败'))
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
      setError(userFacingPageError(cause, '人工修正失败'))
    } finally {
      setBusy('')
    }
  }

  const startNewImport = () => {
    abandonActiveScan()
    goManageView('import')
    setKind('local')
    setPath(config?.local_root || '')
    setProvider('pan115')
    setRemoteRoot(config?.openlist_remote_root || '/')
    clearResultState()
  }

  const setSourceInputsFromCard = (card: V4SourceLibraryCard) => {
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
    setKind('openlist')
    setPath('')
    setRemoteRoot(card.source_locator || config?.openlist_remote_root || '/')
  }

  const sourceMetadataFromCard = (card: V4SourceLibraryCard): SourceCardMetadata => ({
    source_display_name: card.display_name,
    source_locator: card.source_locator,
    playback_locator: card.playback_locator,
    source_route_id: card.route_id,
  })

  const resumeSourceCard = async (card: V4SourceLibraryCard) => {
    abandonActiveScan()
    clearResultState()
    setSourceInputsFromCard(card)
    goManageView('import')
    setError('')

    if (card.scan?.status === 'cancelled') {
      // 已取消的 scan 没有可恢复的后台任务；保留其来源配置，回到来源步骤由
      // 用户显式重新扫描，避免点击卡片后又被旧 scan 的终态错误打断。
      setScanTask(null)
      return
    }

    if (card.scan && (card.phase === 'scan' || isActiveScanStatus(card.scan.status))) {
      const runId = ++scanRunRef.current
      const isCurrentRun = () => scanRunRef.current === runId
      const sourceMetadata = sourceMetadataFromCard(card)
      setBusy('scan')
      setScanTask({ ...card.scan })
      try {
        let completedState: DurableScanState | null = null
        while (true) {
          await new Promise((resolve) => window.setTimeout(resolve, 900))
          if (!isCurrentRun()) return
          const state = await mediaV4Api.durableScan(card.scan.scan_id) as DurableScanState
          if (!isCurrentRun()) return
          setScanTask(scanTaskFromState(state))
          if (state.status === 'completed') {
            completedState = state
            break
          }
          if (state.status === 'failed' || state.status === 'cancelled') {
            throw new Error(state.error || (state.status === 'cancelled' ? '扫描已取消' : '来源扫描失败'))
          }
        }
        if (!completedState || !isCurrentRun()) return

        // 扫描完成与 draft 落盘是两个后台边界；短暂重试一次，避免把正常竞态
        // 错误地显示成“扫描完成但没有结果”。
        let draft: Awaited<ReturnType<typeof mediaV4Api.drafts>>['drafts'][number] | undefined
        for (let attempt = 0; attempt < 5 && !draft; attempt += 1) {
          const drafts = await mediaV4Api.drafts()
          if (!isCurrentRun()) return
          draft = drafts.drafts.find((item) => item.scan_id === card.scan?.scan_id && item.root_id === (completedState?.root_id || card.root_id))
          if (!draft && attempt < 4) await new Promise((resolve) => window.setTimeout(resolve, 200))
        }
        if (!draft) throw new Error('扫描已完成，但识别结果仍在整理，请稍后从来源卡重试')

        const evidenceResult = await mediaV4Api.revisionEvidence(draft.revision_id)
        if (!isCurrentRun()) return
        const evidenceCount = completedState.evidence_count ?? draft.evidence_count ?? evidenceResult.entries.length
        const scanMode = card.last_scan_mode as 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full'
        setRevisionId(draft.revision_id)
        setScan({
          root_id: completedState.root_id || card.root_id,
          scan_id: card.scan.scan_id,
          entries: evidenceResult.entries,
          evidence_count: evidenceCount,
          scan_mode: scanMode,
          source_mode: card.source_mode,
          source_metadata: sourceMetadata,
        })
        const previewResult = await mediaV4Api.preview({
          revision_id: draft.revision_id,
          root_id: completedState.root_id || card.root_id,
          scan_id: card.scan.scan_id,
          entries: [],
          allow_empty: false,
          source_mode: card.source_mode,
          ...sourceMetadata,
        })
        if (!isCurrentRun()) return
        setPreview(previewResult)
        setScanTask(null)
        setWorkflowStage('review')
      } catch (cause) {
        if (isCurrentRun()) setError(userFacingPageError(cause, '无法恢复该来源的扫描进度'))
      } finally {
        if (isCurrentRun()) {
          setScanTask(null)
          setBusy('')
        }
      }
      return
    }

    const resumeRunId = scanRunRef.current
    const isCurrentResume = () => scanRunRef.current === resumeRunId
    if (card.revision_id && (card.phase === 'review' || card.revision_state === 'draft')) {
      setBusy('preview')
      try {
        const evidenceResult = await mediaV4Api.revisionEvidence(card.revision_id)
        if (!isCurrentResume()) return
        const scanId = card.scan?.scan_id || evidenceResult.entries[0]?.scan_id || ''
        const sourceMetadata = sourceMetadataFromCard(card)
        const previewResult = await mediaV4Api.preview({
          revision_id: card.revision_id,
          root_id: card.root_id,
          scan_id: scanId,
          entries: [],
          allow_empty: false,
          source_mode: card.source_mode,
          ...sourceMetadata,
        })
        if (!isCurrentResume()) return
        setRevisionId(card.revision_id)
        setScan({
          root_id: card.root_id,
          scan_id: scanId,
          entries: evidenceResult.entries,
          evidence_count: evidenceResult.entries.length || card.evidence_count,
          scan_mode: card.last_scan_mode as 'local' | 'tree_snapshot' | 'tree_baseline' | 'incremental' | 'full',
          source_mode: card.source_mode,
          source_metadata: sourceMetadata,
        })
        setPreview(previewResult)
        setWorkflowStage('review')
      } catch (cause) {
        if (isCurrentResume()) setError(userFacingPageError(cause, '无法读取该来源的识别结果'))
      } finally {
        if (isCurrentResume()) setBusy('')
      }
      return
    }

    if (!card.revision_id) {
      setError('该来源尚未生成可恢复的识别结果，请重新扫描')
      return
    }
    try {
      const result = await mediaV4Api.status(card.revision_id)
      if (!isCurrentResume()) return
      setRevisionId(card.revision_id)
      setJobs(result.jobs)
      if (result.progress) setExecuteProgress(result.progress)
      setScan(null)
      setPreview(null)
      localStorage.setItem(ACTIVE_REVISION_KEY, card.revision_id)
      setWorkflowStage('execute')
    } catch (cause) {
      if (isCurrentResume()) setError(userFacingPageError(cause, '无法读取该媒体库的导入进度'))
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
      setError(userFacingPageError(cause, '任务重试失败'))
    } finally {
      setRetryingJobId('')
    }
  }

  const searchMetadataRecovery = async (workId: string, query = '') => {
    const workTitle = executeProgress?.work_units.find((unit) => unit.work_id === workId)?.title || '该作品'
    setMetadataRecoveryBusy(workId)
    setError('')
    try {
      const result = await mediaV4Api.metadataSearch({ work_id: workId, query })
      setMetadataRecovery({ workId, workTitle, candidates: result.candidates })
      setMetadataRecoveryQuery(query || workTitle)
    } catch (cause) {
      setError(userFacingPageError(cause, '无法搜索在线作品候选'))
    } finally {
      setMetadataRecoveryBusy('')
    }
  }

  const retryMetadataRecovery = async (workId: string) => {
    setMetadataRecoveryBusy(workId)
    setError('')
    try {
      await mediaV4Api.metadataRetry(workId)
      const activeRevisionId = executeProgress?.revision_id || revisionId
      if (activeRevisionId) {
        const result = await mediaV4Api.status(activeRevisionId)
        setJobs(result.jobs)
        if (result.progress) setExecuteProgress(result.progress)
      }
      void refreshSourceCards()
    } catch (cause) {
      setError(userFacingPageError(cause, '媒体信息重试失败'))
    } finally {
      setMetadataRecoveryBusy('')
    }
  }

  const retryArtifacts = async (workId: string) => {
    setMetadataRecoveryBusy(workId)
    setError('')
    try {
      await mediaV4Api.metadataArtifactsRetry(workId)
      const activeRevisionId = executeProgress?.revision_id || revisionId
      if (activeRevisionId) {
        const result = await mediaV4Api.status(activeRevisionId)
        setJobs(result.jobs)
        if (result.progress) setExecuteProgress(result.progress)
      }
      void refreshSourceCards()
    } catch (cause) {
      setError(userFacingPageError(cause, '重新下载媒体图片失败'))
    } finally {
      setMetadataRecoveryBusy('')
    }
  }

  const confirmMetadataRecovery = async (candidate: MetadataRecoveryCandidate) => {
    if (!metadataRecovery) return
    setMetadataRecoveryBusy(candidate.candidate_id)
    setError('')
    try {
      await mediaV4Api.metadataConfirm({
        work_id: metadataRecovery.workId,
        candidate_id: candidate.candidate_id,
      })
      const result = await mediaV4Api.status(revisionId)
      setJobs(result.jobs)
      if (result.progress) setExecuteProgress(result.progress)
      setMetadataRecovery(null)
      void refreshSourceCards()
    } catch (cause) {
      setError(userFacingPageError(cause, '确认在线作品失败'))
    } finally {
      setMetadataRecoveryBusy('')
    }
  }

  const providerLabel = (provider: string) => {
    if (provider === 'local') return '本地'
    if (provider === 'pan115') return '115 网盘'
    if (provider === 'baidu') return '百度网盘'
    if (provider === 'quark') return '夸克网盘'
    return '其他来源'
  }

  // P-005 返工 11.13-2：内容来源与导入方式严格分层。
  // providerLabel 是内容来源（本地/115/百度/夸克）；sourceMethodLabel 是导入方式。
  const sourceMethodLabel = (card: V4SourceLibraryCard) => {
    if (card.source_mode === 'local') return '本地扫描'
    if (card.source_mode === 'tree_snapshot') return '目录树 TXT'
    if (card.source_mode === 'tree_openlist') return '目录树基线 · OpenList 更新'
    if (card.source_mode === 'openlist_full') return 'OpenList 扫描'
    // 兼容回填前的旧卡：按遗留 ingest_method 展示，不作为新判断依据。
    return card.ingest_method === 'local_scan' ? '本地扫描' : card.ingest_method === 'directory_tree' ? '目录树 TXT' : 'OpenList 扫描'
  }

  const prepareSourceUpdate = (card: V4SourceLibraryCard) => {
    abandonActiveScan()
    clearResultState()
    goManageView('import')
    setSourceInputsFromCard(card)
  }

  const terminateSourceTask = async (card: V4SourceLibraryCard) => {
    const task = card.active_task
    if (!task || !task.can_cancel) return
    setError('')
    setSourceCards((cards) => cards.map((item) => item.root_id === card.root_id && item.active_task
      ? { ...item, active_task: { ...item.active_task, status: 'cancelling', label: item.active_task.kind === 'scan' ? '正在终止扫描' : '正在终止任务', cancel_requested: true, can_cancel: false } }
      : item))
    try {
      if (task.kind === 'scan' && card.scan?.scan_id) {
        await mediaV4Api.cancelDurableScan(card.scan.scan_id)
      } else if (task.revision_id) {
        await mediaV4Api.cancelImport(task.revision_id)
      }
      await refreshSourceCards()
    } catch {
      setError('终止任务失败，请稍后重试')
      await refreshSourceCards()
    }
  }

  const hideSourceCard = async () => {
    if (!sourceCardPendingDelete || sourceCardDeleting) return
    const card = sourceCardPendingDelete
    setSourceCardDeleting(true)
    setError('')
    try {
      await mediaV4Api.hideSourceLibraryCard(card.root_id)
      setSourceCards((cards) => cards.filter((item) => item.root_id !== card.root_id))
      setSourceCardPendingDelete(null)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        // 卡片列表是轮询快照；来源根可能已被维护任务移除。对 DELETE
        // 来说它已经达到目标状态，直接清掉本地快照，避免用户被 404 卡住。
        setSourceCards((cards) => cards.filter((item) => item.root_id !== card.root_id))
        setSourceCardPendingDelete(null)
        return
      }
      setError(userFacingPageError(cause, '移除来源卡失败，请稍后重试'))
    } finally {
      setSourceCardDeleting(false)
    }
  }

  const openSourceCardRename = (card: V4SourceLibraryCard) => {
    setSourceCardRenameValue(card.display_name)
    setSourceCardRenameError('')
    setSourceCardPendingRename(card)
  }

  const renameSourceCard = async () => {
    if (!sourceCardPendingRename || sourceCardRenaming) return
    const displayName = sourceCardRenameValue.trim()
    if (!displayName) {
      setSourceCardRenameError('请填写来源名称')
      return
    }
    setSourceCardRenaming(true)
    setSourceCardRenameError('')
    try {
      const result = await mediaV4Api.renameSourceLibraryCard(sourceCardPendingRename.root_id, displayName)
      setSourceCards((cards) => cards.map((item) => item.root_id === result.root_id
        ? { ...item, display_name: result.display_name }
        : item))
      setSourceCardPendingRename(null)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        // 重命名提交时列表也可能已经过期；根记录不存在时，移除本地快照，
        // 避免用户继续在一个已经失效的来源卡上反复提交。
        setSourceCards((cards) => cards.filter((item) => item.root_id !== sourceCardPendingRename.root_id))
        setSourceCardPendingRename(null)
        return
      }
      setSourceCardRenameError(userFacingPageError(cause, '保存名称失败，请稍后重试'))
    } finally {
      setSourceCardRenaming(false)
    }
  }

  return (
    <div className="media-flow-page media-v4-page">
      <header className="media-flow-header">
        <div className="media-flow-title">
          <span>媒体管理</span>
          <h1>{pageMode === 'overview' ? '媒体库' : pageMode === 'maintenance' ? '媒体库维护' : '导入媒体'}</h1>
          {pageMode !== 'import' && <p>{pageMode === 'overview'
            ? '查看已导入的来源或添加新的媒体库。'
            : '按来源清理媒体库数据与受控生成物。'}</p>}
        </div>
        <div className="media-v4-header-actions" role="toolbar" aria-label="媒体库操作">
          {pageMode === 'overview' ? (
            <>
              <div className="media-v4-header-secondary-actions">
                <Button className="media-v4-header-secondary-button media-v4-header-command" appearance="outline" icon={<ShieldCheckmark24Regular />} onClick={() => goManageView('maintenance')}>媒体库维护</Button>
              </div>
              <div className="media-v4-header-primary-actions">
                <Button className="media-primary-command media-v4-header-command" appearance="primary" icon={<Add24Regular />} onClick={startNewImport}>导入媒体</Button>
              </div>
            </>
          ) : (
            <div className="media-v4-header-secondary-actions">
              <Button className="media-v4-header-back-button media-v4-header-secondary-button" appearance="outline" icon={<ArrowLeft24Regular />} onClick={() => goManageView('overview')}>返回媒体管理</Button>
            </div>
          )}
          {pageMode === 'import' && showReset && <div className="media-v4-header-primary-actions"><Button className="media-v4-new-import" appearance="subtle" icon={<ArrowReset24Regular />} onClick={startNewImport}>重新开始</Button></div>}
        </div>
      </header>

      {pageMode === 'overview' && <>
      {(sourceCardsLoading || sourceCards.length > 0) && <section className="media-v4-source-libraries" aria-label="已导入媒体库">
        <div className="media-v4-source-libraries-heading">
          <h2>已导入来源</h2>
          <Button appearance="subtle" icon={<ArrowSync24Regular />} disabled={sourceCardsLoading} onClick={() => void refreshSourceCards()}>刷新状态</Button>
        </div>
        {sourceCardsLoading && sourceCards.length === 0 ? <div className="media-v4-source-card-loading"><Spinner size="small" />正在读取媒体库…</div> : <div className="media-v4-source-library-grid">
          {sourceCards.map((card) => {
            const activeTask = card.active_task
            const active = Boolean(activeTask)
            const executionTerminated = card.overall_status === 'cancelled' && card.phase === 'execute'
            const cancelledLabel = executionTerminated ? '任务已终止' : '扫描已取消'
            const stateLabel = activeTask?.status === 'cancelling' ? '正在终止…'
              : activeTask?.status === 'queued' ? '等待中'
              : activeTask ? '进行中'
                : card.phase === 'review' && card.attention_count === 0 ? '待确认'
                : card.overall_status === 'needs_attention' ? '需要处理'
                  : card.overall_status === 'cancelled' ? cancelledLabel : '已完成'
            const progressLabel = activeTask
              ? `${activeTask.label}${activeTask.percent == null ? '' : ` · ${activeTask.percent}%`}`
              : card.overall_status === 'cancelled' ? cancelledLabel
                : card.phase === 'review' ? '识别结果待确认'
                  // 失联/失败的扫描任务显示后端的中断与失败文案，不冒充仍在处理。
                  : card.overall_status === 'needs_attention' && card.phase === 'scan' && card.progress?.message
                    ? card.progress.message
                      : card.overall_status === 'needs_attention' ? '有任务需要处理'
                        : '上次导入已处理完毕'
            const resumeLabel = activeTask ? '查看进度' : card.overall_status === 'cancelled' ? (executionTerminated ? '查看执行结果' : '重新扫描') : card.overall_status === 'needs_attention' && card.phase === 'scan' ? '重新扫描' : card.phase === 'review' ? '查看识别结果' : card.can_resume ? '查看进度' : '查看上次导入'
            const resumeIcon = activeTask?.kind === 'scan' || card.phase === 'review' ? <DocumentText24Regular /> : <Database24Regular />
            return <article className={`media-v4-library-source-card ${active ? 'active' : 'settled'}`} key={card.root_id}>
              <div className="media-v4-source-card-identity">
                <div className="media-v4-source-card-heading">
                <div className="media-v4-library-source-card-top"><MediaProviderIcon provider={providerVisualFor(card.provider)} size={20} /><span className="media-v4-source-card-provider-label">{providerLabel(card.provider)}</span><span className="media-v4-source-card-method-label">{sourceMethodLabel(card)}</span></div>
                <span className={`media-v4-source-card-state media-v4-source-card-state-${card.overall_status ?? 'completed'}`}>{stateLabel}</span>
                </div>
                <strong title={card.display_name}>{card.display_name}</strong>
                {card.display_path && card.display_path !== card.display_name && <span className="media-v4-source-card-locator" title={card.source_locator}>{card.display_path}</span>}
                {card.last_error && card.overall_status === 'needs_attention' && <span className="media-v4-source-card-error" role="alert">{card.last_error}</span>}
              </div>
              <div className="media-v4-source-card-scale">
                <div className="media-v4-source-card-stats"><span>{card.work_count} 部作品</span><span>{card.asset_count} 个文件</span></div>
                <div className="media-v4-source-card-status">
                <div className="media-v4-source-card-progress" role="status"><span>{progressLabel}</span></div>
                {card.attention_count > 0 && <span className="media-v4-source-card-attention">有 {card.attention_count} 个待处理事项</span>}
                {(card.relation_pending_count ?? 0) > 0 && <span className="media-v4-source-card-notice">{card.relation_pending_count} 项关联信息待补全，不影响入库和播放</span>}
                </div>
                <div className="media-v4-source-card-actions" role="group" aria-label="导入与更新">
                  <Button className={`media-v4-source-card-action ${card.can_resume ? 'primary' : 'secondary'}`} appearance={card.can_resume ? 'primary' : 'secondary'} icon={resumeIcon} onClick={() => void resumeSourceCard(card)}>{resumeLabel}</Button>
                  {activeTask?.can_cancel || activeTask?.status === 'cancelling'
                    ? <Button className="media-v4-source-card-action secondary" appearance="secondary" icon={<Dismiss24Regular />} disabled={activeTask?.status === 'cancelling'} onClick={() => void terminateSourceTask(card)}>{activeTask?.status === 'cancelling' ? '正在终止…' : '终止任务'}</Button>
                    : <Button className={`media-v4-source-card-action ${card.can_resume ? 'secondary' : 'primary'}`} appearance={card.can_resume ? 'secondary' : 'primary'} icon={<ArrowSync24Regular />} disabled={active} onClick={() => prepareSourceUpdate(card)}>检查更新</Button>}
                </div>
                {!active && <div className="media-v4-source-card-actions media-v4-source-card-management" role="group" aria-label="来源卡管理">
                  {!active && <Button className="media-v4-source-card-action secondary" appearance="secondary" icon={<Edit24Regular />} aria-label={`重命名来源卡：${card.display_name}`} onClick={() => openSourceCardRename(card)}>重命名</Button>}
                  {!active && <Button className="media-v4-source-card-action media-v4-source-card-action-danger" appearance="secondary" icon={<Delete24Regular />} aria-label={`删除来源卡：${card.display_name}`} onClick={() => setSourceCardPendingDelete(card)}>删除来源卡</Button>}
                </div>}
              </div>
            </article>
          })}
        </div>}
      </section>}
      {!sourceCardsLoading && sourceCards.length === 0 && (
        <section className="media-v4-source-empty" aria-label="空媒体库">
          <div className="media-v4-empty"><strong>还没有导入任何媒体库</strong><span>点击“导入媒体”开始建立你的第一个来源。</span></div>
        </section>
      )}
      </>}

      {pageMode === 'maintenance' && (
        <section className="media-stage-shell media-v4-stage-panel media-v4-maintenance-panel">
          <LibraryMaintenancePanel
            busy={busy !== ''}
            onPreview={(scope) => mediaV4Api.maintenancePreview(scope)}
            onConfirm={async (preview) => {
              const result = await mediaV4Api.maintenanceConfirm({ preview_id: preview.preview_id, scope: preview.scope, digest: preview.digest })
              void refreshSourceCards()
              return result
            }}
            onResume={async (previewId) => {
              const result = await mediaV4Api.maintenanceResume(previewId)
              void refreshSourceCards()
              return result
            }}
          />
        </section>
      )}

      {error && <MessageBar className="media-v4-message" intent="error"><MessageBarBody>{error}</MessageBarBody></MessageBar>}

      {pageMode === 'import' && <>
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
      {workflowStage === 'source' && <section className="media-stage-shell media-v4-stage-panel media-v4-source-card">
        <div className="media-stage-header">
          <div className="media-stage-heading">
            <span className="media-stage-icon" aria-hidden="true"><FolderOpen24Regular /></span>
            <div>
              <span className="media-stage-eyebrow">第 1 步</span>
              <h2>选择媒体来源</h2>
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
                <span><strong>{option.label}</strong></span>
                <span className="media-v4-source-check" aria-hidden="true">{selected && <CheckmarkCircle24Filled />}</span>
              </button>
            )
          })}
        </div>

        <div className={`media-v4-config-panel media-v4-workspace workspace-${kind}`}>
          <div className="media-v4-config-heading">
            <strong>{kind === 'local' ? '选择本机文件夹' : kind === 'tree' ? '导入目录树清单' : kind === 'hybrid' ? '建立基线并检查后续变化' : '浏览 OpenList 目录'}</strong>
            {kind !== 'tree' && <span>{kind === 'local'
              ? '扫描本机文件；网盘挂载请使用目录树或 OpenList。'
              : kind === 'hybrid'
                ? '先导入 TXT 基线，再用 OpenList 检查变化。'
                : '进入目标文件夹后扫描当前目录。'}</span>}
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
                <div className="media-v4-field-copy"><strong>内容来源</strong></div>
                <ProviderPicker value={provider} onChange={setProvider} />
                <a className="media-v4-provider-link" href={providerOption.website} target="_blank" rel="noreferrer">{providerOption.websiteLabel}<span aria-hidden="true">↗</span></a>
              </div>
              <div className="media-v4-field-block">
                <div className="media-v4-field-copy"><strong>目录树 TXT 文件</strong><span>支持 115、百度、夸克和 OpenList 导出的清单。</span></div>
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
                <div><strong>{selectedRemoteRoute ? '已匹配播放路径' : '当前目录尚未匹配播放路径'}</strong><span>{selectedRemoteRoute ? '播放位置由已保存的 OpenList 路由推导。' : '请先进入一个已配置内容来源的目录，才能开始扫描。'}</span></div>
                {!selectedRemoteRoute && <Button appearance="subtle" onClick={goSettings}>配置来源路由</Button>}
              </div>
              <div className="media-v4-command-row">
                {openlistBaseline?.has_confirmed_baseline ? (
                  <>
                    <div><strong>已有已确认基线，默认增量更新</strong><span>增量只核对新增和变化目录；远端异常时可执行完整校验。</span></div>
                    <div className="media-v4-command-buttons">
                      <Button appearance="secondary" icon={<ScanObject24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource('full')}>{busy === 'scan' ? <Spinner size="tiny" /> : remoteBrowsing ? '正在切换目录' : '完整校验'}</Button>
                      <Button aria-label="增量扫描" className="media-primary-command" appearance="primary" icon={<ArrowSync24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource('incremental')}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : remoteBrowsing ? '正在切换目录' : '增量扫描'}</Button>
                      {busy === 'scan' && scanTask && <Button appearance="secondary" icon={<Dismiss24Regular />} onClick={() => void cancelScanTask()}>取消扫描</Button>}
                    </div>
                  </>
                ) : (
                  <>
                    <div><strong>首次完整扫描建立基线</strong><span>确认本次导入后，此目录将解锁增量更新；当前还不能增量扫描。</span></div>
                    <div className="media-v4-command-buttons">
                      <Button appearance="secondary" icon={<ArrowSync24Regular />} disabled>增量扫描</Button>
                      <Button aria-label="完整扫描并建立基线" className="media-primary-command" appearance="primary" icon={<ScanObject24Regular />} disabled={busy !== '' || remoteBrowsing || !canScan} onClick={() => void scanSource()}>{busy === 'scan' ? <><Spinner size="tiny" />正在扫描</> : remoteBrowsing ? '正在切换目录' : '完整扫描并建立基线'}</Button>
                      {busy === 'scan' && scanTask && <Button appearance="secondary" icon={<Dismiss24Regular />} onClick={() => void cancelScanTask()}>取消扫描</Button>}
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
                <div><strong>{selectedRemoteRoute ? '已匹配播放路径' : '当前目录尚未匹配播放路径'}</strong><span>{selectedRemoteRoute ? '播放位置由已保存的 OpenList 路由推导。' : '请先进入一个已配置内容来源的目录。'}</span></div>
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
        {busy === 'scan' && scanTask && (
          <MessageBar className="media-v4-message media-v4-inline-scan-status" intent="info">
            <MessageBarBody>
              <div className="media-v4-scan-progress">
                <span><Spinner size="tiny" />{scanTask.stage_label || '正在扫描媒体来源'}</span>
                <span>{scanTask.progress == null ? '正在建立来源清单…' : `已完成 ${scanTask.progress}%`}</span>
                <ProgressBar
                  value={scanTask.total_count ? Math.min(1, (scanTask.processed_count || 0) / scanTask.total_count) : undefined}
                  max={1}
                  aria-label="来源扫描进度"
                />
              </div>
            </MessageBarBody>
          </MessageBar>
        )}
      </section>}

      {workflowStage === 'review' && scan && <section className="media-stage-shell media-v4-stage-panel media-v4-review-card">
        <div className="media-stage-header">
          <div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><DocumentText24Regular /></span><div><span className="media-stage-eyebrow">第 2 步</span><h2>检查识别结果</h2><p>已扫描 {scan.evidence_count} 个媒体条目。默认按作品摘要检查，需要处理的条目会置顶。</p></div></div>
          {!preview && <Button appearance="secondary" disabled={busy !== '' || (scan.evidence_count === 0 && !allowEmpty)} onClick={() => void buildPreview()}>{busy === 'preview' ? <Spinner size="tiny" /> : '生成识别预览'}</Button>}
        </div>
        {scan.scan_mode === 'incremental' && <MessageBar intent="info"><MessageBarBody>本次使用 OpenList 增量核对：请求 {scan.scan_stats?.requested_directories || 0} 个目录，其中滚动抽查 {scan.scan_stats?.rolling_verified || 0} 个、变化优先核对 {scan.scan_stats?.changed_directories || 0} 个。</MessageBarBody></MessageBar>}
        {scan.scan_mode === 'tree_baseline' && <MessageBar intent="info"><MessageBarBody>TXT 基线已建立。确认本次导入后，再扫描同一 OpenList 目录时会自动进入风险受控增量核对。</MessageBarBody></MessageBar>}
        {!preview && <div className="media-v4-empty">{scan.evidence_count === 0 ? <Checkbox checked={allowEmpty} onChange={(_, data) => setAllowEmpty(Boolean(data.checked))} label="我确认该来源当前确实为空，并允许移除它先前导入的媒体" /> : '正在生成识别结果…'}</div>}
        {preview && <>
          {preview.issues.length > 0 && <MessageBar intent="warning"><MessageBarBody>发现 {preview.issues.length} 个需要人工处理的问题；未解决前不能确认。</MessageBarBody></MessageBar>}
          <V4RecognitionSummary
            preview={preview}
            issues={preview.issues}
            evidenceEntries={scan?.entries ?? []}
            overrideDrafts={overrideDrafts}
            onOpenMaintenance={() => goManageView('maintenance')}
            busy={busy !== ''}
            onOverrideChange={(evidenceId, draft) => setOverrideDrafts((current) => ({ ...current, [evidenceId]: draft }))}
            onApplyOverride={(evidenceId) => void applyOverride(evidenceId)}
          />
          <div className="media-v4-command-row media-v4-confirm-row"><div><strong>{preview.issues.length > 0 ? '需要先处理识别问题' : '识别结果可以建立媒体库'}</strong><span>确认后将生成镜像、获取媒体信息并更新媒体库。</span></div><Button className="media-primary-command" appearance="primary" icon={<Database24Regular />} disabled={busy !== '' || preview.issues.length > 0 || preview.status === 'confirmed'} onClick={() => void confirmRevision()}>{busy === 'confirm' ? <><Spinner size="tiny" />正在建立</> : preview.status === 'confirmed' ? '已建立媒体库' : '确认并建立媒体库'}</Button></div>
        </>}
      </section>}

      {workflowStage === 'execute' && <section className="media-stage-shell media-v4-stage-panel media-v4-jobs-card">
        <div className="media-stage-header media-v4-execute-stage-header">
          <div className="media-stage-heading"><span className="media-stage-icon" aria-hidden="true"><Database24Regular /></span><div><span className="media-stage-eyebrow">第 3 步</span><h2>建立媒体库</h2><p>可以离开此页面；返回后会继续显示当前导入进度。</p></div></div>
          <div className="media-v4-execute-stage-header-side">
            {executeProgress && <div className="media-v4-execution-header-summary" aria-label="建立媒体库总体进度">
              <strong>{executeProgress.work_units.length}</strong>
              <span>部作品</span>
              <span aria-hidden="true">·</span>
              <span>{executionMirrorDone}/{executionMirrorTotal} 已完成镜像</span>
            </div>}
            {preview && <details className="media-v4-review-details"><summary>查看本次识别摘要</summary><div className="media-v4-review-details-body">
              <V4RecognitionSummary
                preview={preview}
                issues={preview.issues}
                evidenceEntries={scan?.entries ?? []}
                overrideDrafts={overrideDrafts}
                onOpenMaintenance={() => goManageView('maintenance')}
                busy={busy !== ''}
                onOverrideChange={(evidenceId, draft) => setOverrideDrafts((current) => ({ ...current, [evidenceId]: draft }))}
                onApplyOverride={(evidenceId) => void applyOverride(evidenceId)}
              />
            </div></details>}
          </div>
        </div>
        {executeProgress ? (
          <V4ExecutionProgressView
            progress={executeProgress}
            busyRetryId={retryingJobId}
            onRetry={(jobId: string) => { const job = jobs.find((item) => item.job_id === jobId); if (job) void retryJob(job) }}
            resolvingWorkId={metadataRecoveryBusy}
            onResolveMetadata={(workId: string) => { void searchMetadataRecovery(workId) }}
            onRetryMetadata={(workId: string) => { void retryMetadataRecovery(workId) }}
            onRetryArtifacts={(workId: string) => { void retryArtifacts(workId) }}
            fetchWorkDetail={mediaV4Api.workExecutionDetail}
          />
        ) : (
          <div className="media-v4-empty">正在读取执行进度…</div>
        )}
      </section>}
      {metadataRecovery && (
        <Dialog open onOpenChange={(_, data) => { if (!data.open && metadataRecoveryBusy === '') setMetadataRecovery(null) }}>
          <DialogSurface className="media-v4-metadata-dialog" aria-describedby={undefined}>
            <DialogBody>
              <DialogTitle>确认“{metadataRecovery.workTitle}”的在线作品</DialogTitle>
              <DialogContent>
                <p>系统没有得到唯一匹配。请从候选中选择正确作品；确认后会立即继续获取媒体信息。</p>
                <div className="media-v4-metadata-search-row">
                  <Input aria-label="搜索作品名称" value={metadataRecoveryQuery} onChange={(_, data) => setMetadataRecoveryQuery(data.value)} />
                  <Button appearance="secondary" disabled={metadataRecoveryBusy !== '' || !metadataRecoveryQuery.trim()} onClick={() => { void searchMetadataRecovery(metadataRecovery.workId, metadataRecoveryQuery.trim()) }}>
                    重新搜索
                  </Button>
                </div>
                <div className="media-v4-metadata-candidate-list">
                  {metadataRecovery.candidates.length > 0 ? [...metadataRecovery.candidates].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).map((candidate) => (
                    <Button key={candidate.candidate_id} appearance="secondary" className="media-v4-metadata-candidate" disabled={metadataRecoveryBusy !== ''} onClick={() => { void confirmMetadataRecovery(candidate) }}>
                      <span className="media-v4-metadata-candidate-head">
                        <strong>{candidate.title || candidate.original_title || '未命名候选'}</strong>
                        {candidate.recommended && <span className="media-v4-metadata-candidate-badge">推荐</span>}
                      </span>
                      <span className="media-v4-metadata-candidate-meta">
                        {[candidate.original_title, candidate.year ? String(candidate.year) : '', candidate.media_type === 'movie' ? '电影' : '剧集'].filter(Boolean).join(' · ')}
                      </span>
                      {(candidate.score != null || (candidate.reasons?.length ?? 0) > 0) && (
                        <span className="media-v4-metadata-candidate-evidence">
                          {candidate.score != null && <span className="media-v4-metadata-candidate-score">匹配度 {Math.round(candidate.score)}</span>}
                          {(candidate.reasons ?? []).slice(0, 2).map((reason) => <span key={reason}>{reason}</span>)}
                        </span>
                      )}
                    </Button>
                  )) : <div className="media-v4-empty">没有找到候选。请调整名称后重新搜索，或检查 TMDB 配置。</div>}
                </div>
              </DialogContent>
              <DialogActions>
                <Button appearance="secondary" disabled={metadataRecoveryBusy !== ''} onClick={() => setMetadataRecovery(null)}>暂不处理</Button>
              </DialogActions>
            </DialogBody>
          </DialogSurface>
        </Dialog>
      )}
      </>}
      {sourceCardPendingDelete && (
        <Dialog modalType="modal" open onOpenChange={(_, data) => { if (!data.open && !sourceCardDeleting) setSourceCardPendingDelete(null) }}>
          <DialogSurface
            className="media-v4-source-card-delete-dialog"
            backdrop={{ className: 'media-v4-source-card-delete-backdrop' }}
            aria-describedby={undefined}
          >
            <FluentProvider theme={getKumiFluentTheme(appearanceMode)} className="media-v4-source-card-delete-dialog-provider">
              <DialogBody className="media-v4-source-card-dialog-body">
                <DialogTitle className="media-v4-source-card-dialog-title">移除来源卡？</DialogTitle>
                <DialogContent className="media-v4-source-card-dialog-content">
                  <p>要从“已导入来源”中移除“{sourceCardPendingDelete.display_name}”吗？</p>
                  <p>只会隐藏这张来源卡，不会删除媒体库、镜像、资料或观看状态；以后重新扫描同一来源时，卡片会再次出现。</p>
                </DialogContent>
                <DialogActions className="media-v4-source-card-dialog-actions">
                  <Button className="media-v4-source-card-dialog-cancel" appearance="secondary" disabled={sourceCardDeleting} onClick={() => setSourceCardPendingDelete(null)}>取消</Button>
                  <Button className="media-v4-source-card-dialog-confirm media-v4-source-card-dialog-danger" appearance="primary" icon={sourceCardDeleting ? <Spinner size="tiny" /> : <Delete24Regular />} disabled={sourceCardDeleting} onClick={() => void hideSourceCard()}>{sourceCardDeleting ? '正在移除…' : '移除'}</Button>
                </DialogActions>
              </DialogBody>
            </FluentProvider>
          </DialogSurface>
        </Dialog>
      )}
      {sourceCardPendingRename && (
        <Dialog modalType="modal" open onOpenChange={(_, data) => { if (!data.open && !sourceCardRenaming) setSourceCardPendingRename(null) }}>
          <DialogSurface className="media-v4-source-card-rename-dialog" backdrop={{ className: 'media-v4-source-card-rename-backdrop' }}>
            <FluentProvider theme={getKumiFluentTheme(appearanceMode)} className="media-v4-source-card-rename-dialog-provider">
              <DialogBody className="media-v4-source-card-dialog-body">
                <DialogTitle className="media-v4-source-card-dialog-title">重命名来源卡</DialogTitle>
                <DialogContent className="media-v4-source-card-dialog-content">
                  <p>这只会修改媒体库中的显示名称，不会改变文件夹、网盘目录或已有媒体。</p>
                  <Field label="来源名称" hint="仅修改来源卡显示名，不会改动实际目录。" validationMessage={sourceCardRenameError || undefined} validationState={sourceCardRenameError ? 'error' : 'none'}>
                    <Input aria-label="来源名称" value={sourceCardRenameValue} maxLength={200} autoFocus onChange={(_, data) => setSourceCardRenameValue(data.value)} />
                  </Field>
                </DialogContent>
                <DialogActions className="media-v4-source-card-dialog-actions">
                  <Button className="media-v4-source-card-dialog-cancel" appearance="secondary" disabled={sourceCardRenaming} onClick={() => setSourceCardPendingRename(null)}>取消</Button>
                  <Button className="media-v4-source-card-dialog-confirm" appearance="primary" icon={sourceCardRenaming ? <Spinner size="tiny" /> : <Edit24Regular />} disabled={sourceCardRenaming} onClick={() => void renameSourceCard()}>{sourceCardRenaming ? '正在保存…' : '保存名称'}</Button>
                </DialogActions>
              </DialogBody>
            </FluentProvider>
          </DialogSurface>
        </Dialog>
      )}
    </div>
  )
}
