/** P-004 专项回归：来源图标映射、draft 恢复、OpenList 缓存与竞态保护。 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import MediaManagementPage from '../../src/pages/MediaManagementPage'
import OpenListFolderBrowser from '../../src/components/media/OpenListFolderBrowser'
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
vi.mock('../../src/platform/folderPicker', () => ({ pickDirectoryTreeFile: vi.fn(), pickFolder: vi.fn() }))
vi.mock('../../src/stores/mediaWorkflow', () => ({
  useMediaWorkflowStore: (selector: (state: unknown) => unknown) => selector({
    pendingDroppedTreePath: '',
    consumeDroppedTreePath: vi.fn(),
  }),
}))

const routes = [
  { route_id: 'route-115', label: '115 网盘', remote_prefix: '/115', provider_id: 'pan115', enabled: true, local_path: 'K:\\115网盘', local_available: true },
]

function browseResult(path: string, cacheStatus: 'fresh' | 'stale' | 'none' = 'fresh') {
  return {
    path,
    parent_path: path === '/' ? null : '/',
    remote_root: '/',
    entries: [{ name: 'Anime', is_dir: true, size: null, modified: null, remote_path: '/115/Anime' }],
    page: 1,
    per_page: 100,
    total: 1,
    has_more: false,
    cache: { cached: cacheStatus !== 'none', status: cacheStatus, refreshing: false, refresh_failed: false, fetched_at: Date.now(), expires_at: null },
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  useUiStore.setState({ page: 'manage', manageView: 'overview', navigationHistory: [], forwardHistory: [], canGoBack: false, canGoForward: false, query: '' })
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] })
  api.preview.mockResolvedValue({ revision_id: 'rev', status: 'draft', works: [], episodes: [], work_assets: [], issues: [] })
  api.status.mockResolvedValue({ revision_id: 'rev', status: 'confirmed', jobs: [] })
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.revisionEvidence.mockResolvedValue({ revision_id: 'rev-draft', status: 'draft', entries: [{ evidence_id: 'ev-1', scan_id: 'scan-draft', root_id: 'root-draft', source_key: 'Show/Show.S01E01.mkv', relative_path: 'Show/Show.S01E01.mkv', entry_kind: 'video', provider: 'pan115', source_locator: 'Show/Show.S01E01.mkv', playback_locator: 'K:\\115网盘\\Show\\Show.S01E01.mkv', ingest_method: 'openlist_api' }] })
  api.startDurableScan.mockResolvedValue({ scan_id: 'scan-d', root_id: 'root-d', scan_mode: 'full', status: 'running' })
  api.durableScan.mockResolvedValue({ scan_id: 'scan-d', root_id: 'root-d', status: 'completed', started_at: '', finished_at: '', error: '', entries: [] })
  api.cancelDurableScan.mockResolvedValue({ scan_id: 'scan-d', status: 'cancelling' })
  api.openlistStatus.mockResolvedValue({ root_id: 'root-115', remote_root: '/115/Anime', source_mode: '', last_scan_mode: '', has_confirmed_baseline: false })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘', baidu_root: 'K:\\百度网盘', local_root: 'D:\\Media',
    openlist_configured: true, openlist_remote_root: '/', openlist_mount_root: 'K:\\', openlist_routes: routes,
  })
  openlist.getRoutes.mockResolvedValue({ routes })
  openlist.browse.mockImplementation(async (path: string) => browseResult(path || '/'))
})

test('提供商选择使用专用矢量图标，不再渲染文字占位', async () => {
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '导入媒体' }))
  fireEvent.click(screen.getByRole('button', { name: '目录树 TXT' }))
  await screen.findByRole('button', { name: '115 网盘' })

  expect(screen.queryByText('115')).not.toBeInTheDocument()
  expect(screen.queryByText('百')).not.toBeInTheDocument()
  expect(screen.queryByText('夸')).not.toBeInTheDocument()
  const icons = document.querySelectorAll('.media-provider-icon.provider-pan115, .media-provider-icon.provider-baidu, .media-provider-icon.provider-quark')
  expect(icons.length).toBe(3)
  // 可访问名称仍来自可见文字，而不是图标文件名。
  expect(screen.getByRole('button', { name: '115 网盘' })).toBeVisible()
})

test('来源卡使用图标与状态徽标，不再出现文字方块', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [{
      root_id: 'root-115', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
      last_scan_mode: 'full', has_confirmed_baseline: true, overall_status: 'needs_attention', attention_count: 2,
      last_error: '磁盘写入失败', source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画', route_id: 'route-115',
      display_name: '115 动画', revision_id: 'rev-card', revision_status: 'confirmed',
      revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
      evidence_count: 120, work_count: 30, asset_count: 120, can_resume: true,
      job_summary: { total: 3, queued: 0, running: 0, succeeded: 2, failed: 1, cancelled: 0 },
    }],
  })
  render(<MediaManagementPage />)

  expect(await screen.findByText('115 动画')).toBeVisible()
  expect(screen.getByText('需要处理')).toBeVisible()
  expect(screen.getByText('磁盘写入失败')).toBeVisible()
  expect(screen.getByText('有 2 个待处理事项')).toBeVisible()
  expect(document.querySelector('.media-provider-icon.provider-pan115')).not.toBeNull()
  expect(screen.queryByText('115', { selector: '.media-v4-provider-mark' })).not.toBeInTheDocument()
})

test('未确认草稿不进入媒体库概览，只有确认后的来源才建立来源卡', async () => {
  api.drafts.mockResolvedValue({
    drafts: [{
      revision_id: 'rev-draft', root_id: 'root-draft', scan_id: 'scan-draft', created_at: '2026-08-25T00:00:00Z',
      provider: 'pan115', source_mode: 'openlist_full', source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画',
      evidence_count: 12, issue_count: 1,
    }],
  })
  render(<MediaManagementPage />)

  expect(await screen.findByText('还没有导入任何媒体库')).toBeVisible()
  expect(screen.queryByText('待继续导入')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '继续检查识别结果' })).not.toBeInTheDocument()
  expect(api.drafts).not.toHaveBeenCalled()
  expect(api.revisionEvidence).not.toHaveBeenCalled()
})

test('OpenList 缓存过期时展示提示且刷新可获取最新', async () => {
  openlist.browse.mockImplementation(async (path: string) => browseResult(path || '/', 'stale'))
  render(<OpenListFolderBrowser configured initialPath="/" onPathChange={() => undefined} onGoSettings={() => undefined} />)
  expect(await screen.findByText(/缓存的目录列表/)).toBeVisible()

  openlist.browse.mockImplementation(async (path: string) => browseResult(path || '/', 'fresh'))
  fireEvent.click(screen.getByRole('button', { name: '刷新当前层' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledWith('/', 1, true, 100))
  expect(await screen.findByText(/远端最新/)).toBeVisible()
})

test('OpenList 慢响应不覆盖新目录（最新请求获胜）', async () => {
  let resolveSlow: (value: ReturnType<typeof browseResult>) => void = () => undefined
  const slow = new Promise<ReturnType<typeof browseResult>>((resolve) => { resolveSlow = resolve })
  openlist.browse
    .mockReturnValueOnce(Promise.resolve(browseResult('/115', 'fresh'))) // 初始浏览（快，返回 /115）
    .mockReturnValueOnce(slow)                                           // 打开 Anime（慢）
    .mockReturnValueOnce(Promise.resolve(browseResult('/', 'fresh')))    // 面包屑返回根（快）
  render(<OpenListFolderBrowser configured initialPath="/" onPathChange={() => undefined} onGoSettings={() => undefined} />)
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledTimes(2))
  // 慢目录尚未返回时用户点击面包屑返回根目录；快请求先到达。
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))
  await waitFor(() => expect(openlist.browse).toHaveBeenCalledTimes(3))
  await screen.findByText(/当前目录：\//)
  // 慢响应迟到：不得覆盖当前路径。
  resolveSlow(browseResult('/115/Anime-old', 'fresh'))
  await new Promise((resolve) => setTimeout(resolve, 80))
  expect(screen.getByText(/当前目录：\//)).toBeVisible()
  expect(screen.queryByText(/Anime-old/)).not.toBeInTheDocument()
})

test('OpenList 扫描进行中提供取消入口', async () => {
  api.startDurableScan.mockResolvedValue({ scan_id: 'scan-c', root_id: 'root-c', scan_mode: 'full', status: 'running' })
  api.durableScan.mockResolvedValue({ scan_id: 'scan-c', root_id: 'root-c', status: 'running', started_at: '', finished_at: '', error: '', entries: [] })
  render(<MediaManagementPage />)
  fireEvent.click(await screen.findByRole('button', { name: '导入媒体' }))
  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }))
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 Anime' }))
  await screen.findByText(/当前目录：\/115\/Anime/)
  fireEvent.click(screen.getByRole('button', { name: '完整扫描并建立基线' }))
  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalled())
  const cancel = await screen.findByRole('button', { name: '取消扫描' })
  fireEvent.click(cancel)
  await waitFor(() => expect(api.cancelDurableScan).toHaveBeenCalledWith('scan-c'))
})
