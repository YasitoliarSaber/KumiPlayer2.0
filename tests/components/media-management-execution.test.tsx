/** P-003 三阶段状态机 + 紧凑识别摘要 + 作品级执行进度组件测试。 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { V4ExecutionProgress } from '../../src/components/media/V4ExecutionProgress'
import MediaManagementPage from '../../src/pages/MediaManagementPage'
import { useUiStore } from '../../src/stores/ui'

const api = vi.hoisted(() => ({
  scan: vi.fn(),
  preview: vi.fn(),
  confirm: vi.fn(),
  status: vi.fn(),
  overrideEvidence: vi.fn(),
  sourceLibraries: vi.fn(),
  openlistStatus: vi.fn(),
  drafts: vi.fn(),
  revisionEvidence: vi.fn(),
  startDurableScan: vi.fn(),
  durableScan: vi.fn(),
  cancelDurableScan: vi.fn(),
  workExecutionDetail: vi.fn(),
}))
const config = vi.hoisted(() => ({ getConfig: vi.fn() }))
const openlist = vi.hoisted(() => ({ browse: vi.fn(), getRoutes: vi.fn() }))
const tasks = vi.hoisted(() => ({ retry: vi.fn() }))

vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: api }))
vi.mock('../../src/api/config', () => ({ configApi: config }))
vi.mock('../../src/api/openlist', () => ({ openlistApi: openlist }))
vi.mock('../../src/api/tasks', () => ({ tasksApi: tasks }))
vi.mock('../../src/platform/folderPicker', () => ({
  pickDirectoryTreeFile: vi.fn(),
  pickFolder: vi.fn(),
}))
vi.mock('../../src/stores/mediaWorkflow', () => ({
  useMediaWorkflowStore: (selector: (state: unknown) => unknown) => selector({
    pendingDroppedTreePath: '',
    consumeDroppedTreePath: vi.fn(),
  }),
}))

function makeProgress(units: Array<Record<string, unknown>>, overrides: Record<string, unknown> = {}) {
  return {
    revision_id: 'rev-exec',
    revision_status: 'confirmed',
    overall_status: 'running',
    stage_summary: {
      mirror: { status: 'running', total: 40, queued: 39, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
      metadata: { status: 'queued', total: 40, queued: 40, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
      projection: { status: 'queued', total: 1, queued: 1, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
    },
    work_units: units,
    ...overrides,
  }
}

function workUnit(workId: string, title: string, overallStatus: string, extra: Record<string, unknown> = {}) {
  return {
    work_id: workId,
    title,
    media_type: 'tv',
    episode_count: 12,
    asset_count: 13,
    overall_status: overallStatus,
    mirror: { job_id: `mirror-${workId}`, status: overallStatus === 'completed' ? 'succeeded' : overallStatus === 'failed' ? 'failed' : 'queued', attempts: 1, last_error: overallStatus === 'failed' ? '磁盘写入失败' : '' },
    metadata: { job_id: `meta-${workId}`, status: overallStatus === 'completed' ? 'succeeded' : 'queued', attempts: 0, last_error: '' },
    ...extra,
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  api.workExecutionDetail.mockResolvedValue({ has_detail: false })
  useUiStore.setState({ page: 'manage', manageView: 'overview', navigationHistory: [], forwardHistory: [], canGoBack: false, canGoForward: false, query: '' })
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] })
  api.preview.mockResolvedValue({
    revision_id: 'rev-preview',
    status: 'draft',
    works: [],
    episodes: [],
    work_assets: [],
    issues: [],
  })
  api.confirm.mockResolvedValue({ revision_id: 'rev-exec', status: 'confirmed', jobs: [] })
  api.status.mockResolvedValue({
    revision_id: 'rev-exec',
    status: 'confirmed',
    jobs: [],
    progress: makeProgress([]),
  })
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.revisionEvidence.mockResolvedValue({ revision_id: 'rev', status: 'draft', entries: [] })
  api.startDurableScan.mockResolvedValue({ scan_id: 'scan-durable', root_id: 'root', scan_mode: 'full', status: 'running' })
  api.durableScan.mockResolvedValue({ scan_id: 'scan-durable', root_id: 'root', status: 'completed', started_at: '', finished_at: '', error: '', entries: [] })
  api.cancelDurableScan.mockResolvedValue({ scan_id: 'scan-durable', status: 'cancelling' })
  api.openlistStatus.mockResolvedValue({ root_id: 'r', remote_root: '/', source_mode: '', last_scan_mode: '', has_confirmed_baseline: false })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘',
    baidu_root: 'K:\\百度网盘',
    local_root: 'D:\\Media',
    openlist_configured: true,
    openlist_remote_root: '/',
    openlist_mount_root: 'K:\\',
    openlist_routes: [],
  })
  openlist.getRoutes.mockResolvedValue({ routes: [] })
  tasks.retry.mockResolvedValue({ status: 'pending' })
})

async function scanLocal(revisionId: string) {
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '导入媒体' }))
  const input = await screen.findByRole('textbox', { name: '本机媒体文件夹' })
  fireEvent.change(input, { target: { value: 'D:\\Anime' } })
  api.durableScan.mockResolvedValue({
    scan_id: 'scan-durable', root_id: 'root', status: 'completed', started_at: '',
    finished_at: '', error: '', evidence_count: 1, entries: [],
  })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))
  await waitFor(() => expect(api.preview).toHaveBeenCalled())
  expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    revision_id: expect.stringMatching(/^rev-/),
  }))
  expect(api.preview).toHaveBeenCalledWith(expect.objectContaining({ entries: [] }))
  api.preview.mockResolvedValue({
    revision_id: revisionId,
    status: 'draft',
    works: [{ work_key: 'w1', preferred_title: '测试作品', year: 2024, media_type: 'tv', source_evidence_ids: ['ev-1'] }],
    episodes: [{ work_key: 'w1', episode_key: 'w1-1', local_season_number: 1, local_episode_number: 1, absolute_episode_number: null, season_kind: 'regular', episode_kind: 'regular', special_number: null, edition_key: 'default', asset_evidence_ids: ['ev-1'] }],
    work_assets: [],
    issues: [],
  })
}

test('确认成功后第二步卸载并切换到独立执行阶段', async () => {
  await scanLocal('rev-exec')
  expect(await screen.findByRole('heading', { name: '检查识别结果' })).toBeVisible()
  expect(screen.getByRole('button', { name: '确认并建立媒体库' })).toBeEnabled()
  api.status.mockResolvedValue({
    revision_id: 'rev-exec', status: 'confirmed', jobs: [],
    progress: makeProgress([workUnit('w1', '测试作品', 'running_mirror')]),
  })
  fireEvent.click(screen.getByRole('button', { name: '确认并建立媒体库' }))
  await waitFor(() => expect(screen.getByRole('heading', { name: '建立媒体库' })).toBeVisible())
  expect(screen.queryByRole('heading', { name: '检查识别结果' })).not.toBeInTheDocument()
  expect(await screen.findByText('测试作品')).toBeVisible()
  // 步骤条：第三步 active
  expect(screen.getByRole('navigation', { name: '导入步骤' }).querySelector('li[aria-current="step"]')?.textContent).toContain('建立媒体库')
})

test('识别预览聚合作品摘要，不逐集平铺', async () => {
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '导入媒体' }))
  const input = await screen.findByRole('textbox', { name: '本机媒体文件夹' })
  fireEvent.change(input, { target: { value: 'D:\Anime' } })
  api.durableScan.mockResolvedValue({
    scan_id: 'scan-durable', root_id: 'root', status: 'completed', started_at: '',
    finished_at: '', error: '', evidence_count: 1, entries: [],
  })
  api.preview.mockResolvedValue({
    revision_id: 'rev-big', status: 'draft',
    works: [{ work_key: 'w1', preferred_title: '大长篇', year: 2024, media_type: 'tv', source_evidence_ids: [] }],
    episodes: Array.from({ length: 520 }, (_, index) => ({
      work_key: 'w1', episode_key: `w1-${index}`, local_season_number: 1, local_episode_number: index + 1,
      absolute_episode_number: null, season_kind: 'regular', episode_kind: 'regular', special_number: null,
      edition_key: 'default', asset_evidence_ids: [`ev-${index}`],
    })),
    work_assets: [],
    issues: [],
  })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))
  await waitFor(() => expect(screen.getByText('大长篇')).toBeVisible())
  // 520 集不会渲染成 520 个集级标签。
  expect(screen.queryByText(/S01E/)).not.toBeInTheDocument()
  expect(screen.getByText(/共 520 个视频/)).toBeVisible()
})

test('执行进度只显示三个总体阶段与作品单元，不出现内部 UUID', async () => {
  const units = Array.from({ length: 40 }, (_, index) => workUnit(`w${index}`, `作品${index}`, index === 0 ? 'running_mirror' : 'waiting_metadata'))
  api.status.mockResolvedValue({
    revision_id: 'rev-exec', status: 'confirmed', jobs: [],
    progress: makeProgress(units),
  })
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-exec', latest_revision_id: 'rev-exec', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-exec')
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))

  await waitFor(() => expect(screen.getByText('作品0')).toBeVisible())
  // 每个用户阶段只出现一次。
  expect(screen.getAllByText('生成镜像文件')).toHaveLength(1)
  expect(screen.getAllByText('获取媒体信息')).toHaveLength(1)
  expect(screen.getAllByText('更新媒体库')).toHaveLength(1)
  // 40 个作品单元。
  expect(screen.getAllByRole('article')).toHaveLength(40)
  // 不泄漏内部 UUID / job_type。
  expect(screen.queryByText(/mirror-w0/)).not.toBeInTheDocument()
  expect(screen.queryByText(/materialize_mirror/)).not.toBeInTheDocument()
  // 后端持久标题与状态文案。
  expect(screen.getByText('正在生成镜像')).toBeVisible()
  expect(screen.getAllByText('等待获取媒体信息').length).toBe(39)
})

test('失败作品显示原因并可精确重试', async () => {
  const units = [
    workUnit('w-ok', '正常作品', 'completed'),
    workUnit('w-bad', '失败作品', 'failed'),
  ]
  api.status.mockResolvedValue({
    revision_id: 'rev-exec', status: 'confirmed', jobs: [
      { job_id: 'mirror-w-bad', job_type: 'materialize_mirror', revision_id: 'rev-exec', work_id: 'w-bad', status: 'failed', idempotency_key: 'ik', attempts: 1, last_error: '磁盘写入失败' },
    ],
    progress: makeProgress(units, { overall_status: 'needs_attention' }),
  })
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-exec', latest_revision_id: 'rev-exec', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-exec')
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))

  await waitFor(() => expect(screen.getByText('失败作品')).toBeVisible())
  const failedCard = screen.getByText('失败作品').closest('article')!
  // 错误原因与重试在展开后的作品单元里，不进入普通主列表。
  expect(within(failedCard).queryByText('磁盘写入失败')).not.toBeInTheDocument()
  fireEvent.click(within(failedCard).getByRole('button', { name: /失败作品/ }))
  expect(await within(failedCard).findByText('磁盘写入失败')).toBeVisible()
  fireEvent.click(within(failedCard).getByRole('button', { name: /重试/ }))
  await waitFor(() => expect(tasks.retry).toHaveBeenCalledWith('mirror-w-bad'))
})

test('完成作品保留可见的作品摘要，避免完成页只剩总数', async () => {
  const detail = {
    work: { title: '已完成作品', provider: 'tmdb', provider_id: '42', metadata_state: 'ready' },
    mirror: { status: 'succeeded', artifact_count: 2 }, seasons: [], has_detail: true,
    episode_total: 2, next_episode_offset: 1,
    episodes: [{ episode_id: 'ep-first', season_number: 1, episode_number: 1, display_title: '第一页剧集' }],
  }
  api.workExecutionDetail.mockImplementation((_revisionId, _workId, offset = 0) => Promise.resolve(offset === 1
    ? { ...detail, next_episode_offset: null, episodes: [{ episode_id: 'ep-next', season_number: 1, episode_number: 2, display_title: '下一页剧集' }] }
    : detail))
  const units = [
    workUnit('w-run', '运行中作品', 'running_mirror'),
    workUnit('w-done', '已完成作品', 'completed'),
  ]
  api.status.mockResolvedValue({
    revision_id: 'rev-exec', status: 'confirmed', jobs: [],
    progress: makeProgress(units),
  })
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-exec', latest_revision_id: 'rev-exec', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-exec')
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))

  await waitFor(() => expect(screen.getByText('运行中作品')).toBeVisible())
  expect(screen.getByText('已完成 1 部')).toBeVisible()
  expect(await screen.findByText('已完成作品')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /已完成作品/ }))
  fireEvent.click(await screen.findByRole('button', { name: /显示更多剧集/ }))
  expect(await screen.findByText('下一页剧集')).toBeVisible()
  expect(screen.getByText('第一页剧集')).toBeVisible()
  expect(api.workExecutionDetail).toHaveBeenCalledWith('rev-exec', 'w-done', 1)
})

test('媒体信息需要人工处理的作品不显示为已完成', async () => {
  const units = [
    workUnit('w-review', '待人工确认', 'needs_attention', { metadata: { job_id: 'meta-w-review', status: 'succeeded', attempts: 1, last_error: '' } }),
  ]
  api.status.mockResolvedValue({
    revision_id: 'rev-exec', status: 'confirmed', jobs: [],
    progress: makeProgress(units, { overall_status: 'needs_attention' }),
  })
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-exec', latest_revision_id: 'rev-exec', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-exec')
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))

  await waitFor(() => expect(screen.getByText('待人工确认')).toBeVisible())
  expect(screen.getByText('需要处理')).toBeVisible()
  expect(screen.queryByText('已完成')).not.toBeInTheDocument()
  expect(screen.queryByText('任务进行中，完成后自动折叠到“已完成”。')).not.toBeInTheDocument()
})

test('作品运行中转为等待确认时自动展开恢复入口', () => {
  const props = {
    busyRetryId: '',
    onRetry: vi.fn(),
    resolvingWorkId: '',
    onResolveMetadata: vi.fn(),
  }
  const unit = workUnit('w-review', '待人工确认', 'running_metadata')
  const { rerender } = render(
    <V4ExecutionProgress progress={makeProgress([unit])} {...props} />,
  )

  expect(screen.queryByRole('button', { name: '选择正确作品' })).not.toBeInTheDocument()

  rerender(
    <V4ExecutionProgress
      progress={makeProgress([
        {
          ...unit,
          overall_status: 'needs_attention',
          metadata_state: 'waiting_review',
          metadata_reason: '在线媒体信息没有唯一匹配，需要确认正确作品后继续。',
          metadata: { job_id: 'meta-w-review', status: 'succeeded', attempts: 1, last_error: '' },
        },
      ], { overall_status: 'needs_attention' })}
      {...props}
    />,
  )

  expect(screen.getByRole('button', { name: '选择正确作品' })).toBeVisible()
})

test('已终止的第三步执行显示终态而不是准备中', () => {
  render(
    <V4ExecutionProgress
      progress={makeProgress([workUnit('w-cancelled', '用户已终止', 'cancelled')], {
        overall_status: 'cancelled',
        stage_summary: {
          mirror: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
          metadata: { status: 'cancelled', total: 1, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 1 },
          projection: { status: 'cancelled', total: 1, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 1 },
        },
      })}
      busyRetryId=""
      onRetry={vi.fn()}
      resolvingWorkId=""
      onResolveMetadata={vi.fn()}
    />,
  )

  expect(screen.getByText('任务已终止')).toBeVisible()
  expect(screen.queryByText('正在准备任务')).not.toBeInTheDocument()
})

test('图片产物缺失时恢复入口改为重新下载媒体图片', () => {
  const retry = vi.fn()
  const work = {
    title: '缺图作品',
    provider: 'tmdb',
    provider_id: '42',
    media_type: 'movie',
    metadata_state: 'failed',
    metadata_reason: '媒体资料已获取，但部分图片下载或发布失败。',
    metadata_reason_code: 'artifact_incomplete',
    metadata_recovery_action: 'retry_metadata',
  }
  render(<V4ExecutionProgress
    progress={makeProgress([workUnit('w-artifact', '缺图作品', 'needs_attention', work)])}
    busyRetryId="" onRetry={vi.fn()} resolvingWorkId="" onResolveMetadata={vi.fn()} onRetryMetadata={retry}
  />)

  // 图片缺失不能被说成“重新获取媒体信息”，否则用户会以为整条在线资料都要重跑。
  expect(screen.queryByRole('button', { name: '重新获取媒体信息' })).not.toBeInTheDocument()
  const button = screen.getByRole('button', { name: '重新下载媒体图片' })
  fireEvent.click(button)
  expect(retry).toHaveBeenCalledWith('w-artifact')
})

test('图片产物缺失时显示非阻断提示与重新下载入口', () => {
  const artifacts = vi.fn()
  const unit = workUnit('w-degraded', '缺图作品', 'completed', { artifact_state: 'degraded' })
  render(<V4ExecutionProgress
    progress={makeProgress([unit], { overall_status: 'completed' })}
    busyRetryId="" onRetry={vi.fn()} resolvingWorkId="" onResolveMetadata={vi.fn()} onRetryArtifacts={artifacts}
  />)

  expect(screen.getByText('部分图片未下载成功，可重新下载；不影响浏览和播放。')).toBeVisible()
  const button = screen.getByRole('button', { name: '重新下载媒体图片' })
  fireEvent.click(button)
  expect(artifacts).toHaveBeenCalledWith('w-degraded')
})

test.each(['check_settings', 'retry_metadata', 'review_identity'])('恢复入口在详情加载后保持唯一：%s', async (action) => {
  const retry = vi.fn()
  const resolve = vi.fn()
  const work = { title: '恢复作品', provider: '', provider_id: '', media_type: 'tv', metadata_state: action === 'review_identity' ? 'waiting_review' : 'waiting_metadata', metadata_reason: '需要处理', metadata_recovery_action: action }
  render(<V4ExecutionProgress
    progress={makeProgress([workUnit('w-recover', '恢复作品', 'needs_attention', work)])}
    busyRetryId="" onRetry={vi.fn()} resolvingWorkId="" onResolveMetadata={resolve} onRetryMetadata={retry}
    fetchWorkDetail={vi.fn().mockResolvedValue({ work, mirror: { status: 'succeeded', artifact_count: 1 }, seasons: [], episodes: [], episode_total: 0, has_detail: true })}
  />)
  await screen.findByText('作品信息')
  const buttons = screen.getAllByRole('button', { name: action === 'review_identity' ? '选择正确作品' : '重新获取媒体信息' })
  expect(buttons).toHaveLength(1)
  fireEvent.click(buttons[0])
  expect(action === 'review_identity' ? resolve : retry).toHaveBeenCalledWith('w-recover')
})

test('季度失败摘要保留特别篇并按 choose_candidate 显示恢复入口', async () => {
  const resolve = vi.fn()
  const work = {
    title: '季度异常作品',
    provider: 'tmdb',
    provider_id: '42',
    media_type: 'tv',
    metadata_state: 'source_unavailable',
    metadata_reason: '在线作品资料不可用，请重新选择正确的在线作品。',
    metadata_recovery_action: 'choose_candidate',
  }
  const fetchWorkDetail = vi.fn().mockResolvedValue({
    work,
    mirror: { status: 'succeeded', artifact_count: 1 },
    metadata_job_status: 'succeeded',
    seasons: [{ season_number: 0, season_kind: 'special', title: '', episode_count: 1 }],
    scrape: {
      metadata_state: 'source_unavailable',
      metadata_reason: '特别篇在线集数未匹配，请核对本地季度/集号；在线资料更新后可重试。',
      metadata_recovery_action: 'retry_metadata',
      title: '季度异常作品', original_title: '', year: null, plot: '', rating: null, runtime: null,
      genres: [], studios: [], premiered: '',
      season_results: [{
        local_season_number: 0,
        provider_season_number: 0,
        status: 'partial',
        reason_code: 'episode_not_found',
        failure_stage: 'season_detail',
        retryable: false,
      }],
    },
    episodes: [],
    episode_total: 0,
    has_detail: true,
  })

  render(<V4ExecutionProgress
    progress={makeProgress([workUnit('w-season-recover', '季度异常作品', 'needs_attention', work)])}
    busyRetryId=""
    onRetry={vi.fn()}
    resolvingWorkId=""
    onResolveMetadata={resolve}
    fetchWorkDetail={fetchWorkDetail}
  />)

  expect(await screen.findByText('作品信息')).toBeVisible()
  expect(screen.getByRole('button', { name: '选择正确作品' })).toBeVisible()
  expect(screen.getByText('特别篇 · 1 集')).toBeVisible()
  expect(screen.getByText('特别篇：在线集数未匹配')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '选择正确作品' }))
  expect(resolve).toHaveBeenCalledWith('w-season-recover')
})

function renderProgressWithDetail(units: Array<Record<string, unknown>>, overrides: Record<string, unknown>, fetchWorkDetail: ReturnType<typeof vi.fn>) {
  return render(
    <V4ExecutionProgress
      progress={makeProgress(units, overrides)}
      busyRetryId=""
      onRetry={vi.fn()}
      resolvingWorkId=""
      onResolveMetadata={vi.fn()}
      fetchWorkDetail={fetchWorkDetail}
    />,
  )
}

test('展开已完成作品后读取并显示作品信息、镜像结果与剧集结果', async () => {
  const fetchWorkDetail = vi.fn().mockResolvedValue({
    revision_id: 'rev-exec',
    work_id: 'w-done',
     work: { title: '完成作品', media_type: 'tv', provider: 'tmdb', provider_id: '12345', metadata_state: 'ready', metadata_reason: '' },
     mirror: { status: 'succeeded', error: '', artifact_count: 1, artifacts: [{ file_name: 'S01E01-abc.strm', status: 'published' }] },
     metadata_job_status: 'succeeded',
     seasons: [{ season_number: 1, season_kind: 'regular', title: '', episode_count: 1 }],
     scrape: {
       metadata_state: 'ready', title: '完成作品', original_title: 'Completed Work', year: 2024,
       plot: '这是作品级刮削简介。', rating: 8.2, runtime: 24,
       candidate_decision: { decision: 'auto_adopted', reason: '高分候选', selected_score: 98, ranked_candidates: [] },
     },
     episodes: [{
       episode_id: 'ep-1', season_number: 1, season_kind: 'regular', episode_number: 1,
       display_title: '本地第一集', scraped_title: '远程第一集全名', scraped_plot: '这一集的刮削简介。',
       provider_episode_id: '9001', runtime: 24, still_url: '', mapped: true,
       file_name: 'Show.S01E01.mkv', playback_ready: true,
     }],
    episode_total: 1,
    episodes_truncated: false,
    has_detail: true,
  })
  renderProgressWithDetail(
    [workUnit('w-done', '完成作品', 'completed')],
    {
      overall_status: 'completed',
      stage_summary: {
        mirror: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        metadata: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        projection: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
      },
    },
    fetchWorkDetail,
  )
  fireEvent.click(screen.getByRole('button', { name: /完成作品/ }))

  expect(await screen.findByText('作品信息')).toBeVisible()
  // 用户反馈：镜像结果不需要展示（"知道生成了就行"），因此该区块已被移除。
  expect(screen.queryByText('镜像结果')).toBeNull()
  expect(screen.getByText(/剧集结果（1 集）/)).toBeVisible()
  expect(screen.getByText('季度结构')).toBeVisible()
  expect(screen.getByText('第 1 季 · 1 集')).toBeVisible()
  expect(screen.getByText('刮削结果')).toBeVisible()
  expect(screen.getByText('这是作品级刮削简介。')).toBeVisible()
  expect(screen.getByText(/自动采用 · 候选分 98/)).toBeVisible()
  expect(fetchWorkDetail).toHaveBeenCalledWith('rev-exec', 'w-done')
  // 一条完整集名 + 季集号 + 阶段结果。
  expect(screen.getByText('远程第一集全名')).toBeVisible()
  expect(screen.getByText('S01E01')).toBeVisible()
  expect(screen.queryByText('S01E01-abc.strm')).not.toBeInTheDocument()
  expect(screen.getByText('1 个播放文件')).toBeVisible()
  expect(screen.getByText('这一集的刮削简介。')).toBeVisible()
  expect(screen.getByText('TMDB ID 9001')).toBeVisible()
  expect(screen.getByText('已映射')).toBeVisible()
})

test('详情读取失败可在同一展开面板内重新读取，空详情仍使用缓存', async () => {
  const fetchWorkDetail = vi.fn()
    .mockRejectedValueOnce(new Error('后端不可用'))
    .mockResolvedValueOnce({
      revision_id: 'rev-exec',
      work_id: 'w-empty',
      work: { title: '空详情作品', media_type: 'tv', provider: '', provider_id: '', metadata_state: '', metadata_reason: '' },
      mirror: { status: 'succeeded', error: '', artifact_count: 0, artifacts: [] },
      metadata_job_status: 'succeeded',
      seasons: [],
      episodes: [],
      episode_total: 0,
      episodes_truncated: false,
      has_detail: false,
    })
  renderProgressWithDetail(
    [workUnit('w-empty', '空详情作品', 'completed')],
    {
      overall_status: 'completed',
      stage_summary: {
        mirror: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        metadata: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        projection: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
      },
    },
    fetchWorkDetail,
  )

  // 第一次展开：读取失败 → 明确错误文案。
  fireEvent.click(screen.getByRole('button', { name: /空详情作品/ }))
  expect(await screen.findByText(/执行详情读取失败：后端不可用/)).toBeVisible()

  // 失败不会永久占据请求锁；用户可以在当前展开面板直接重试。
  fireEvent.click(screen.getByRole('button', { name: '重新读取' }))
  await waitFor(() => expect(fetchWorkDetail).toHaveBeenCalledTimes(2))
})
