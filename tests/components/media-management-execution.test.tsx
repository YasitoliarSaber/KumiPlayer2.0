/** P-003 三阶段状态机 + 紧凑识别摘要 + 作品级执行进度组件测试。 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import MediaManagementPage from '../../src/pages/MediaManagementPage'

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
}))
const config = vi.hoisted(() => ({ getConfig: vi.fn() }))
const openlist = vi.hoisted(() => ({ browse: vi.fn(), getRoutes: vi.fn() }))
const tasks = vi.hoisted(() => ({ retry: vi.fn() }))
const goSettings = vi.hoisted(() => vi.fn())

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
vi.mock('../../src/stores/ui', () => ({
  useUiStore: (selector: (state: { goSettings: typeof goSettings }) => unknown) => selector({ goSettings }),
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
  api.scan.mockResolvedValue({
    root_id: 'root-local', scan_id: 'scan-1', entries: [
      { evidence_id: 'ev-1', scan_id: 'scan-1', root_id: 'root-local', source_key: 'Show/Show.S01E01.mkv', relative_path: 'Show/Show.S01E01.mkv', entry_kind: 'video', provider: 'local', source_locator: 'Show/Show.S01E01.mkv', playback_locator: 'D:\\Anime\\Show\\Show.S01E01.mkv', ingest_method: 'local_scan' },
    ],
  })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))
  await waitFor(() => expect(api.preview).toHaveBeenCalled())
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
  api.scan.mockResolvedValue({
    root_id: 'root-local', scan_id: 'scan-1', entries: [
      { evidence_id: 'ev-1', scan_id: 'scan-1', root_id: 'root-local', source_key: 'Show/Show.S01E01.mkv', relative_path: 'Show/Show.S01E01.mkv', entry_kind: 'video', provider: 'local', source_locator: 'Show/Show.S01E01.mkv', playback_locator: 'D:\Anime\Show\Show.S01E01.mkv', ingest_method: 'local_scan' },
    ],
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

test('完成作品默认折叠且支持键盘展开', async () => {
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
  const toggle = screen.getByRole('button', { name: /已完成 1 部/ })
  expect(toggle).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(toggle)
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  expect(await screen.findByText('已完成作品')).toBeVisible()
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
})
