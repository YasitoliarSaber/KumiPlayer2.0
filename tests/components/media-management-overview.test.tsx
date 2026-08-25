/** P-005 媒体管理首页层级与媒体库维护入口测试。 */

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
  openlistStatus: vi.fn(),
  drafts: vi.fn(),
  revisionEvidence: vi.fn(),
  startDurableScan: vi.fn(),
  durableScan: vi.fn(),
  cancelDurableScan: vi.fn(),
  maintenancePreview: vi.fn(),
  maintenanceConfirm: vi.fn(),
}))
const config = vi.hoisted(() => ({ getConfig: vi.fn() }))
const openlist = vi.hoisted(() => ({ browse: vi.fn(), getRoutes: vi.fn() }))
const tasks = vi.hoisted(() => ({ retry: vi.fn() }))
const goSettings = vi.hoisted(() => vi.fn())

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
vi.mock('../../src/stores/ui', () => ({
  useUiStore: (selector: (state: { goSettings: typeof goSettings }) => unknown) => selector({ goSettings }),
}))

function cardFixture(overrides: Record<string, unknown> = {}) {
  return {
    root_id: 'root-115', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
    last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'completed',
    attention_count: 0, last_error: '', source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画',
    route_id: 'route-115', display_name: '115 动画', enabled: 1,
    added_at: '2026-08-20T00:00:00Z', updated_at: '2026-08-24T00:00:00Z',
    revision_id: 'rev-existing', latest_revision_id: 'rev-existing', revision_state: 'completed',
    evidence_count: 120, work_count: 3, asset_count: 120,
    work_previews: [
      { work_id: 'w1', title: '摇曳露营', year: 2024, media_type: 'tv', poster_path: '', episode_count: 12, asset_count_for_source: 12 },
      { work_id: 'w2', title: '孤独摇滚', year: 2022, media_type: 'tv', poster_path: '', episode_count: 12, asset_count_for_source: 12 },
    ],
    progress: { state: 'completed', stage: 'idle', current_work_id: '', current_work_title: '', completed_work_count: 3, total_work_count: 3, percent: 100, message: '上次导入已处理完毕' },
    available_actions: ['inspect', 'update'],
    can_resume: false,
    job_summary: { total: 6, queued: 0, running: 0, succeeded: 6, failed: 0, cancelled: 0 },
    revision_status: 'confirmed', revision_created_at: '2026-08-24T00:00:00Z', confirmed_at: '2026-08-24T00:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] })
  api.preview.mockResolvedValue({ revision_id: 'rev', status: 'draft', works: [], episodes: [], work_assets: [], issues: [] })
  api.status.mockResolvedValue({ revision_id: 'rev', status: 'confirmed', jobs: [] })
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.openlistStatus.mockResolvedValue({ root_id: 'r', remote_root: '/', source_mode: '', last_scan_mode: '', has_confirmed_baseline: false })
  api.maintenancePreview.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', expires_at: '2026-08-25T02:00:00Z',
    root_count: 1, work_count: 2, orphan_work_count: 1, mixed_work_count: 1, asset_count: 3, artifact_count: 2,
    artifact_paths: ['K:\\mirror\\a.jpg'], blocked: false, blocked_jobs: [],
    warnings: ['源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、配置与凭据始终保留'],
    roots: [], orphan_works: ['w-orphan'], mixed_works: ['w-mixed'], digest: 'd'.repeat(64),
  })
  api.maintenanceConfirm.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'completed',
    retired_roots: ['root-baidu'], orphan_works: ['w-orphan'], mixed_works: ['w-mixed'],
    artifact_results: [{ path: 'K:\\mirror\\a.jpg', status: 'removed' }], projection_status: 'ok',
  })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘', baidu_root: 'K:\\百度网盘', local_root: 'D:\\Media',
    openlist_configured: true, openlist_remote_root: '/', openlist_mount_root: 'K:\\', openlist_routes: [],
  })
  openlist.getRoutes.mockResolvedValue({ routes: [] })
  tasks.retry.mockResolvedValue({ status: 'pending' })
})

test('默认进入媒体管理首页，只有来源卡与命令栏，不显示三步导入', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  expect(await screen.findByRole('heading', { name: '媒体库' })).toBeVisible()
  expect(screen.getByRole('button', { name: '导入媒体' })).toBeVisible()
  expect(screen.getByRole('button', { name: '媒体库维护' })).toBeVisible()
  expect(screen.queryByRole('navigation', { name: '导入步骤' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '本地目录' })).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '导入媒体' }))
  expect(await screen.findByRole('navigation', { name: '导入步骤' })).toBeVisible()
  expect(screen.getByRole('button', { name: '返回媒体管理' })).toBeVisible()
})

test('空媒体库只显示空状态与导入主按钮', async () => {
  render(<MediaManagementPage />)

  expect(await screen.findByText('还没有导入任何媒体库')).toBeVisible()
  expect(screen.getAllByRole('button', { name: '导入媒体' }).length).toBeGreaterThan(0)
  expect(screen.queryByRole('navigation', { name: '导入步骤' })).not.toBeInTheDocument()
})

test('来源卡显示真实来源、添加/更新时间、作品预览与用户级进度', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  expect(await screen.findByText('115 动画')).toBeVisible()
  // 内容来源与导入方式严格分层：来源是 115 网盘，OpenList 只是扫描方式。
  expect(screen.getByText('115 网盘')).toBeVisible()
  expect(screen.getByText('OpenList 扫描')).toBeVisible()
  expect(screen.queryByText('OpenList 来源')).not.toBeInTheDocument()
  expect(screen.getByText(/添加于 2026-08-20/)).toBeVisible()
  expect(screen.getByText(/更新于 2026-08-24/)).toBeVisible()
  // 作品预览与规模主信息。
  expect(screen.getByText('摇曳露营')).toBeVisible()
  expect(screen.getByText('孤独摇滚')).toBeVisible()
  expect(screen.getByText('3 部作品')).toBeVisible()
  // 用户级进度而非 raw 任务数。
  expect(screen.getByText('上次导入已处理完毕')).toBeVisible()
  expect(screen.queryByText(/6 个任务/)).not.toBeInTheDocument()
})

test('媒体库维护入口生成预览并分组展示', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))

  expect(await screen.findByRole('heading', { name: '按来源清理' })).toBeVisible()
  expect(screen.getByRole('radio', { name: '全部来源' })).toBeChecked()
  expect(screen.queryByRole('radio', { name: /OpenList/ })).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '生成删除预览' }))
  await waitFor(() => expect(api.maintenancePreview).toHaveBeenCalledWith('all'))
  expect(await screen.findByText(/部将退出媒体库/)).toBeVisible()
  expect(screen.getByText(/部混合来源将保留/)).toBeVisible()
  expect(screen.getByText(/源视频、挂载盘媒体、外部 TXT/)).toBeVisible()
})

test('删除确认调用 maintenance API 并展示逐项结果', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  await screen.findByRole('heading', { name: '按来源清理' })
  fireEvent.click(screen.getByRole('button', { name: '生成删除预览' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认清理此来源' }))

  await waitFor(() => expect(api.maintenanceConfirm).toHaveBeenCalledWith({
    preview_id: 'prev-1', scope: 'all', digest: 'd'.repeat(64),
  }))
  expect(await screen.findByText(/清理完成/)).toBeVisible()
  expect(screen.getByText(/已删除 · K:\\mirror\\a.jpg/)).toBeVisible()
})
