import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
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

const routes = [
  { route_id: 'route-115', label: '115 网盘', remote_prefix: '/115', provider_id: 'pan115', enabled: true, local_path: 'K:\\115网盘', local_available: true },
  { route_id: 'route-quark', label: '夸克动画', remote_prefix: '/Quark/Anime', provider_id: 'quark', enabled: true, local_path: 'K:\\夸克网盘\\动画', local_available: true },
]

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
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
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.revisionEvidence.mockResolvedValue({ revision_id: 'rev', status: 'draft', entries: [] })
  api.startDurableScan.mockResolvedValue({ scan_id: 'scan-durable', root_id: 'root', scan_mode: 'full', status: 'running' })
  api.durableScan.mockResolvedValue({ scan_id: 'scan-durable', root_id: 'root', status: 'completed', started_at: '', finished_at: '', error: '', entries: [] })
  api.cancelDurableScan.mockResolvedValue({ scan_id: 'scan-durable', status: 'cancelling' })
  api.openlistStatus.mockResolvedValue({
    root_id: 'root-115-anime',
    remote_root: '/115/Anime',
    source_mode: '',
    last_scan_mode: '',
    has_confirmed_baseline: false,
  })
  api.status.mockResolvedValue({ revision_id: 'rev-existing', status: 'confirmed', jobs: [] })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘',
    baidu_root: 'K:\\百度网盘',
    local_root: 'D:\\Media',
    openlist_configured: true,
    openlist_remote_root: '/',
    openlist_mount_root: 'K:\\',
    openlist_routes: routes,
  })
  openlist.getRoutes.mockResolvedValue({ routes })
  tasks.retry.mockResolvedValue({ status: 'pending' })
  openlist.browse.mockImplementation(async (path = '') => ({
    path: path || '/',
    parent_path: path && path !== '/' ? '/' : null,
    remote_root: '/',
    entries: path === '/115/Anime'
      ? []
      : [{ name: 'Anime', is_dir: true, size: null, modified: null, remote_path: '/115/Anime' }],
    page: 1,
    per_page: 100,
    total: path === '/115/Anime' ? 0 : 1,
    has_more: false,
    cache: { cached: false, status: 'fresh', refreshing: false, refresh_failed: false, fetched_at: null, expires_at: null },
  }))
})

async function enterImport() {
  fireEvent.click(await screen.findByRole('button', { name: '导入媒体' }))
}

test('本地目录读取设置中的默认路径并只表达本机物理磁盘', async () => {
  render(<MediaManagementPage />)
  await enterImport()

  expect(screen.getByRole('navigation', { name: '导入步骤' })).toBeVisible()
  expect(screen.getByRole('button', { name: '本地目录' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByText('仅扫描本机物理磁盘中的媒体文件')).toBeVisible()
  expect(await screen.findByRole('textbox', { name: '本机媒体文件夹' })).toHaveValue('D:\\Media')
  expect(screen.queryByText('还需要选择媒体目录')).not.toBeInTheDocument()
  expect(screen.queryByText(/已挂载网盘中的媒体文件/)).not.toBeInTheDocument()
})

test('目录树使用真实网盘提供商、官网入口和设置中的播放映射', async () => {
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: '目录树 TXT' }))

  expect(await screen.findByRole('button', { name: '115 网盘' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: '百度网盘' })).toBeVisible()
  expect(screen.getByRole('button', { name: '夸克网盘' })).toBeVisible()
  expect(screen.queryByRole('combobox', { name: '存储来源' })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: '本地挂载根目录（可选）' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: /前往 115 官网/ })).toHaveAttribute('href', 'https://115.com/')
  expect(screen.getByText(/播放路径将使用设置中的/)).toBeVisible()

  fireEvent.click(screen.getByRole('button', { name: '夸克网盘' }))
  expect(screen.getByRole('link', { name: /前往夸克网盘官网/ })).toHaveAttribute('href', 'https://pan.quark.cn/')
  fireEvent.change(screen.getByRole('textbox', { name: '目录树 TXT 文件' }), {
    target: { value: 'K:\\夸克网盘\\动画\\目录树.txt' },
  })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))

  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'tree',
    provider: 'quark',
    source_root: 'K:\\夸克网盘\\动画',
  })))
})

test('OpenList 首次完整扫描建立基线，确认前增量被禁用', async () => {
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))

  expect(await screen.findByRole('region', { name: 'OpenList 目录浏览器' })).toBeVisible()
  // 无已确认基线：主按钮建立基线，增量禁用，且不再提示改用“目录树 + OpenList”。
  expect(screen.queryByText(/增量更新请使用/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '完整扫描并建立基线' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '增量扫描' })).toBeDisabled()
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/115/Anime', 1, false, 100))
  await screen.findByText(/当前目录：\/115\/Anime/)
  fireEvent.click(screen.getByRole('button', { name: '完整扫描并建立基线' }))

  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'full',
  })))
  await waitFor(() => expect(api.durableScan).toHaveBeenCalledWith('scan-durable'))
})

test('已确认基线的 OpenList 默认增量扫描并保留完整校验', async () => {
  api.openlistStatus.mockResolvedValue({
    root_id: 'root-115-anime',
    remote_root: '/115/Anime',
    source_mode: 'openlist_full',
    last_scan_mode: 'incremental',
    has_confirmed_baseline: true,
  })
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await screen.findByText(/当前目录：\/115\/Anime/)

  expect(screen.getByRole('button', { name: '增量扫描' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '完整校验' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '增量扫描' }))
  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'incremental',
  })))
  await waitFor(() => expect(api.durableScan).toHaveBeenCalledWith('scan-durable'))

  api.startDurableScan.mockClear()
  await waitFor(
    () => expect(screen.getByRole('button', { name: '完整校验' })).toBeEnabled(),
    { timeout: 3000 },
  )
  fireEvent.click(screen.getByRole('button', { name: '完整校验' }))
  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'full',
  })))
})

test('OpenList 未进入内容来源路由时不会猜测默认网盘提供商', async () => {
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))

  expect(await screen.findByText('当前目录尚未匹配内容路由')).toBeVisible()
  expect(screen.getByText('请先进入一个已配置内容来源的目录，才能开始扫描。')).toBeVisible()
  expect(screen.getByRole('button', { name: '完整扫描并建立基线' })).toBeDisabled()
})

test('混合入口首次要求 TXT 基线且未确认前禁用增量', async () => {
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: '目录树 + OpenList 增量' }))
  expect(screen.queryByRole('group', { name: '内容来源' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: '首次目录树 TXT 文件' }), {
    target: { value: 'K:\\115网盘\\动画\\目录树.txt' },
  })
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await screen.findByText(/当前目录：\/115\/Anime/)
  // 尚无已确认基线：TXT 是主动作，增量被禁用并说明原因。
  expect(screen.getByRole('button', { name: '增量扫描' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '建立 TXT 基线' }))

  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'hybrid',
    root_path: '/115/Anime',
    tree_file: 'K:\\115网盘\\动画\\目录树.txt',
    provider: 'pan115',
    source_root: 'K:\\115网盘\\Anime',
    scan_mode: 'full',
  })))
})

test('混合入口基线确认后增量扫描发送显式 incremental', async () => {
  api.openlistStatus.mockResolvedValue({
    root_id: 'root-115-anime',
    remote_root: '/115/Anime',
    source_mode: 'tree_openlist',
    last_scan_mode: 'tree_baseline',
    has_confirmed_baseline: true,
  })
  render(<MediaManagementPage />)
  await enterImport()
  fireEvent.click(screen.getByRole('button', { name: '目录树 + OpenList 增量' }))
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await screen.findByText(/当前目录：\/115\/Anime/)
  await waitFor(() => expect(screen.getByRole('button', { name: '增量扫描' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '增量扫描' }))
  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'incremental',
  })))
})

test('本地来源路径有效后提交统一 V4 扫描请求', async () => {
  render(<MediaManagementPage />)
  await enterImport()
  const input = await screen.findByRole('textbox', { name: '本机媒体文件夹' })
  fireEvent.change(input, { target: { value: 'D:\\Anime' } })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))

  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'local',
    root_path: 'D:\\Anime',
    tree_file: '',
    provider: 'local',
    source_root: 'D:\\Anime',
    scan_mode: 'full',
    revision_id: expect.stringMatching(/^rev-/),
  })))
})

test('来源卡展示持久化进度并可恢复查看任务', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-baidu-anime', provider: 'baidu', ingest_method: 'directory_tree',
      source_locator: 'K:\\百度网盘\\动画', playback_locator: 'K:\\百度网盘\\动画', route_id: 'route-baidu',
      display_name: '百度动画库', revision_id: 'rev-existing', revision_status: 'confirmed',
      revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
      evidence_count: 120, work_count: 30, asset_count: 120, can_resume: true,
      job_summary: { total: 4, queued: 2, running: 1, succeeded: 1, failed: 0, cancelled: 0 },
    }],
  })
  render(<MediaManagementPage />)

  expect(await screen.findByRole('region', { name: '已导入媒体库' })).toBeVisible()
  expect(screen.getByText('百度动画库')).toBeVisible()
  expect(screen.getByText('30 部作品')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '查看进度' }))
  await waitFor(() => expect(api.status).toHaveBeenCalledWith('rev-existing'))
})

test('来源卡可以回到同一 OpenList 来源执行更新', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-115-anime', provider: 'pan115', ingest_method: 'openlist_api',
      source_mode: 'openlist_full', has_confirmed_baseline: true, last_scan_mode: 'incremental',
      source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画', route_id: 'route-115',
      display_name: '115 动画', revision_id: 'rev-existing', revision_status: 'confirmed',
      revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
      evidence_count: 120, work_count: 30, asset_count: 120, can_resume: false,
      job_summary: { total: 3, queued: 0, running: 0, succeeded: 3, failed: 0, cancelled: 0 },
    }],
  })
  api.openlistStatus.mockResolvedValue({
    root_id: 'root-115-anime',
    remote_root: '/115/Anime',
    source_mode: 'openlist_full',
    last_scan_mode: 'incremental',
    has_confirmed_baseline: true,
  })
  render(<MediaManagementPage />)

  fireEvent.click(await screen.findByRole('button', { name: '检查更新' }))

  const sourcePicker = screen.getByRole('group', { name: '媒体来源类型' })
  expect(sourcePicker.querySelector('button[aria-label="OpenList"]')).toHaveAttribute('aria-pressed', 'true')
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/115/Anime', 1, false, 100))
  // 已确认基线：来源卡恢复后默认动作是增量扫描。
  expect(await screen.findByRole('button', { name: '增量扫描' })).toBeEnabled()
})

test('任务进度使用用户阶段标签而不是内部 job type', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-existing', latest_revision_id: 'rev-existing', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-existing')
  api.status.mockResolvedValue({
    revision_id: 'rev-existing',
    status: 'confirmed',
    jobs: [{
      job_id: 'job-1',
      job_type: 'materialize_mirror',
      revision_id: 'rev-existing',
      work_id: 'work-1',
      status: 'succeeded',
      idempotency_key: 'materialize_mirror:rev-existing:work-1',
    }],
    progress: {
      revision_id: 'rev-existing',
      revision_status: 'confirmed',
      overall_status: 'completed',
      stage_summary: {
        mirror: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        metadata: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        projection: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
      },
      work_units: [{
        work_id: 'work-1',
        title: '测试作品',
        media_type: 'tv',
        episode_count: 1,
        asset_count: 1,
        overall_status: 'completed',
        mirror: { job_id: 'job-1', status: 'succeeded', attempts: 1, last_error: '' },
        metadata: { job_id: 'job-2', status: 'succeeded', attempts: 1, last_error: '' },
      }],
    },
  })

  render(<MediaManagementPage />)

  // P-005 返工：默认停留来源卡 overview，点击“查看进度”后才进入执行面板。
  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))

  // 用户阶段只出现一次，不按后端 job 数量重复。
  expect(await screen.findByText('生成镜像文件')).toBeVisible()
  expect(screen.getAllByText('生成镜像文件')).toHaveLength(1)
  expect(screen.queryByText('materialize_mirror')).not.toBeInTheDocument()
})

test('失败作品可以从导入进度页精确重试', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-exec', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
      attention_count: 0, last_error: '', source_locator: '/Anime', playback_locator: 'K:\Anime',
      route_id: 'route-115', display_name: '执行中媒体库', enabled: 1,
      added_at: '2026-08-25T00:00:00Z', updated_at: '2026-08-25T00:00:00Z',
      revision_id: 'rev-existing', latest_revision_id: 'rev-existing', revision_state: 'running',
      evidence_count: 10, work_count: 1, asset_count: 1,
      work_previews: [], progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 0, total_work_count: 1, percent: 0, message: '正在生成镜像' },
      available_actions: ['inspect', 'resume'], can_resume: true,
      job_summary: { total: 1, queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
    }],
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-existing')
  api.status.mockResolvedValue({
    revision_id: 'rev-existing',
    status: 'confirmed',
    jobs: [{
      job_id: 'job-failed',
      job_type: 'scrape_work',
      revision_id: 'rev-existing',
      work_id: 'work-1',
      status: 'failed',
      idempotency_key: 'scrape_work:rev-existing:work-1',
      last_error: '网络暂时不可用',
    }],
    progress: {
      revision_id: 'rev-existing',
      revision_status: 'confirmed',
      overall_status: 'needs_attention',
      stage_summary: {
        mirror: { status: 'succeeded', total: 1, queued: 0, running: 0, succeeded: 1, failed: 0, cancelled: 0 },
        metadata: { status: 'failed', total: 1, queued: 0, running: 0, succeeded: 0, failed: 1, cancelled: 0 },
        projection: { status: 'queued', total: 1, queued: 1, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
      },
      work_units: [{
        work_id: 'work-1',
        title: '失败作品',
        media_type: 'tv',
        episode_count: 1,
        asset_count: 1,
        overall_status: 'failed',
        mirror: { job_id: 'job-ok', status: 'succeeded', attempts: 1, last_error: '' },
        metadata: { job_id: 'job-failed', status: 'failed', attempts: 1, last_error: '网络暂时不可用' },
      }],
    },
  })
  render(<MediaManagementPage />)

  fireEvent.click(await screen.findByRole('button', { name: '查看进度' }))
  const card = (await screen.findByText('失败作品')).closest('article')!
  fireEvent.click(within(card).getByRole('button', { name: /失败作品/ }))
  fireEvent.click(await within(card).findByRole('button', { name: /重试/ }))

  await waitFor(() => expect(tasks.retry).toHaveBeenCalledWith('job-failed'))
})

test('只有排队或运行中的来源卡才启动实时轮询', async () => {
  const timeoutSpy = vi.spyOn(window, 'setTimeout')
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-failed', provider: 'baidu', ingest_method: 'directory_tree',
      source_locator: 'K:\\tree.txt', playback_locator: 'K:\\百度网盘', route_id: '',
      display_name: '失败的导入', revision_id: 'rev-failed', revision_status: 'confirmed',
      revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
      evidence_count: 1, work_count: 1, asset_count: 1, can_resume: true,
      job_summary: { total: 3, queued: 0, running: 0, succeeded: 2, failed: 1, cancelled: 0 },
    }],
  })

  render(<MediaManagementPage />)
  expect(await screen.findByText('失败的导入')).toBeVisible()
  expect(timeoutSpy.mock.calls.some((call) => call[1] === 1500)).toBe(false)
  timeoutSpy.mockRestore()
})
