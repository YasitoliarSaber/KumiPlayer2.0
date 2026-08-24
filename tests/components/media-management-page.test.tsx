import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import MediaManagementPage from '../../src/pages/MediaManagementPage'

const api = vi.hoisted(() => ({
  scan: vi.fn(),
  preview: vi.fn(),
  confirm: vi.fn(),
  status: vi.fn(),
  overrideEvidence: vi.fn(),
  sourceLibraries: vi.fn(),
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

const routes = [
  { route_id: 'route-115', label: '115 网盘', remote_prefix: '/115', provider_id: 'pan115', enabled: true, local_path: 'K:\\115网盘', local_available: true },
  { route_id: 'route-quark', label: '夸克动画', remote_prefix: '/Quark/Anime', provider_id: 'quark', enabled: true, local_path: 'K:\\夸克网盘\\动画', local_available: true },
]

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
  api.sourceLibraries.mockResolvedValue({ cards: [] })
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

test('本地目录读取设置中的默认路径并只表达本机物理磁盘', async () => {
  render(<MediaManagementPage />)

  expect(screen.getByRole('navigation', { name: '导入步骤' })).toBeVisible()
  expect(screen.getByRole('button', { name: '本地目录' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByText('仅扫描本机物理磁盘中的媒体文件')).toBeVisible()
  expect(await screen.findByRole('textbox', { name: '本机媒体文件夹' })).toHaveValue('D:\\Media')
  expect(screen.queryByText('还需要选择媒体目录')).not.toBeInTheDocument()
  expect(screen.queryByText(/已挂载网盘中的媒体文件/)).not.toBeInTheDocument()
})

test('目录树使用真实网盘提供商、官网入口和设置中的播放映射', async () => {
  render(<MediaManagementPage />)
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

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'tree',
    provider: 'quark',
    source_root: 'K:\\夸克网盘\\动画',
  })))
})

test('OpenList 使用文件夹浏览并始终执行当前目录完整扫描', async () => {
  render(<MediaManagementPage />)
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))

  expect(await screen.findByRole('region', { name: 'OpenList 目录浏览器' })).toBeVisible()
  expect(screen.queryByRole('switch', { name: '完整扫描' })).not.toBeInTheDocument()
  expect(screen.queryByText('增量扫描')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '扫描此文件夹并识别' })).toBeDisabled()
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/115/Anime', 1, false, 100))
  await screen.findByText(/当前目录：\/115\/Anime/)
  fireEvent.click(screen.getByRole('button', { name: '扫描此文件夹并识别' }))

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'full',
  })))
})

test('OpenList 未进入内容来源路由时不会猜测默认网盘提供商', async () => {
  render(<MediaManagementPage />)
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))

  expect(await screen.findByText('当前目录尚未匹配内容路由')).toBeVisible()
  expect(screen.getByText('请先进入一个已配置内容来源的目录，才能开始扫描。')).toBeVisible()
  expect(screen.getByRole('button', { name: '扫描此文件夹并识别' })).toBeDisabled()
})

test('混合入口分别提供 TXT 基线与显式 OpenList 增量动作', async () => {
  render(<MediaManagementPage />)
  fireEvent.click(screen.getByRole('button', { name: '目录树 + OpenList 增量' }))
  expect(screen.queryByRole('group', { name: '内容来源' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: '首次目录树 TXT 文件' }), {
    target: { value: 'K:\\115网盘\\动画\\目录树.txt' },
  })
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await screen.findByText(/当前目录：\/115\/Anime/)
  fireEvent.click(screen.getByRole('button', { name: '建立 TXT 基线' }))

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'hybrid',
    root_path: '/115/Anime',
    tree_file: 'K:\\115网盘\\动画\\目录树.txt',
    provider: 'pan115',
    source_root: 'K:\\115网盘\\Anime',
    scan_mode: 'auto',
  })))

  api.scan.mockClear()
  fireEvent.click(screen.getByRole('button', { name: '增量扫描' }))
  await waitFor(() => expect(api.scan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    root_path: '/115/Anime',
    provider: 'pan115',
    scan_mode: 'incremental',
  })))
})

test('本地来源路径有效后提交统一 V4 扫描请求', async () => {
  render(<MediaManagementPage />)
  const input = await screen.findByRole('textbox', { name: '本机媒体文件夹' })
  fireEvent.change(input, { target: { value: 'D:\\Anime' } })
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }))

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith({
    source: 'local',
    root_path: 'D:\\Anime',
    tree_file: '',
    provider: 'local',
    source_root: '',
    scan_mode: 'auto',
  }))
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
      source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画', route_id: 'route-115',
      display_name: '115 动画', revision_id: 'rev-existing', revision_status: 'confirmed',
      revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
      evidence_count: 120, work_count: 30, asset_count: 120, can_resume: false,
      job_summary: { total: 3, queued: 0, running: 0, succeeded: 3, failed: 0, cancelled: 0 },
    }],
  })
  render(<MediaManagementPage />)

  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/', 1, false, 100))
  openlist.browse.mockClear()

  fireEvent.click(await screen.findByRole('button', { name: '检查更新' }))

  const sourcePicker = screen.getByRole('group', { name: '媒体来源类型' })
  expect(sourcePicker.querySelector('button[aria-label="OpenList"]')).toHaveAttribute('aria-pressed', 'true')
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/115/Anime', 1, false, 100))
})

test('任务进度使用面向用户的名称而不是内部 job type', async () => {
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
  })

  render(<MediaManagementPage />)

  expect(await screen.findByText('生成镜像文件')).toBeVisible()
  expect(screen.queryByText('materialize_mirror')).not.toBeInTheDocument()
})

test('失败任务可以直接从导入进度页重试', async () => {
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
  })
  render(<MediaManagementPage />)

  fireEvent.click(await screen.findByRole('button', { name: '重试 获取媒体信息' }))

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
