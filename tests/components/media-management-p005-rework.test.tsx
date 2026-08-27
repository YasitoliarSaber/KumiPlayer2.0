/** P-005 返工前端回归：active revision 默认停留 overview、来源/导入方式分层、维护入口默认可见。 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  maintenancePreview: vi.fn(),
  maintenanceConfirm: vi.fn(),
  maintenanceResume: vi.fn(),
  trackingWorks: vi.fn(),
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

function cardFixture(overrides: Record<string, unknown> = {}) {
  return {
    root_id: 'root-115', provider: 'pan115', ingest_method: 'openlist_api', source_mode: 'openlist_full',
    last_scan_mode: 'incremental', has_confirmed_baseline: true, overall_status: 'running',
    attention_count: 0, last_error: '', source_locator: '/115/Anime', playback_locator: 'K:\\115网盘\\动画',
    route_id: 'route-115', display_name: '115 动画', enabled: 1,
    added_at: '2026-08-20T00:00:00Z', updated_at: '2026-08-24T00:00:00Z',
    revision_id: 'rev-active', latest_revision_id: 'rev-active', revision_state: 'running',
    evidence_count: 120, work_count: 3, asset_count: 120,
    work_previews: [{ work_id: 'w1', title: '摇曳露营', year: 2024, media_type: 'tv', poster_path: '', episode_count: 12, asset_count_for_source: 12 }],
    progress: { state: 'running', stage: 'mirror', current_work_id: '', current_work_title: '', completed_work_count: 1, total_work_count: 3, percent: 33, message: '正在生成镜像' },
    available_actions: ['inspect', 'resume'], can_resume: true,
    job_summary: { total: 6, queued: 0, running: 6, succeeded: 0, failed: 0, cancelled: 0 },
    ...overrides,
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  useUiStore.setState({ page: 'manage', manageView: 'overview', navigationHistory: [], forwardHistory: [], canGoBack: false, canGoForward: false, query: '' })
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] })
  api.preview.mockResolvedValue({ revision_id: 'rev', status: 'draft', works: [], episodes: [], work_assets: [], issues: [] })
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.openlistStatus.mockResolvedValue({ root_id: 'r', remote_root: '/', source_mode: '', last_scan_mode: '', has_confirmed_baseline: false })
  api.maintenancePreview.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', created_at: '2026-08-25T00:00:00Z', expires_at: '2026-08-25T02:00:00Z',
    root_ids: ['root-baidu'], root_count: 1, work_count: 2, orphan_work_count: 1, mixed_work_count: 1, asset_count: 3, artifact_count: 2,
    artifact_summaries: ['root-baidu/a.jpg'], blocked: false, blocked_job_count: 0, blocked_job_types: [],
    history_count: 1, progress_count: 1, tracking_count: 1,
    warnings: [], root_names: [{ root_id: 'root-baidu', provider: 'baidu' }], digest: 'd'.repeat(64),
  })
  api.maintenanceConfirm.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'completed',
    retired_root_count: 1, root_names: [{ root_id: 'root-baidu', provider: 'baidu' }],
    orphan_work_count: 1, mixed_work_count: 1, artifact_count: 1,
    artifact_results: [{ path: 'root-baidu/a.jpg', status: 'removed' }], projection_status: 'ok',
  })
  api.maintenanceResume.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'completed',
    retired_root_count: 1, root_names: [{ root_id: 'root-baidu', provider: 'baidu' }],
    orphan_work_count: 1, mixed_work_count: 1, artifact_count: 1,
    artifact_results: [{ path: 'root-baidu/a.jpg', status: 'removed', reused: true }], projection_status: 'ok',
  })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘', baidu_root: 'K:\\百度网盘', local_root: 'D:\\Media',
    openlist_configured: true, openlist_remote_root: '/', openlist_mount_root: 'K:\\', openlist_routes: [],
  })
  openlist.getRoutes.mockResolvedValue({ routes: [] })
  tasks.retry.mockResolvedValue({ status: 'pending' })
})

test('有 active revision 时默认停留来源卡 overview，点查看进度才进入执行', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.status.mockResolvedValue({
    revision_id: 'rev-active', status: 'confirmed', jobs: [],
    progress: {
      revision_id: 'rev-active', revision_status: 'confirmed', overall_status: 'running',
      stage_summary: {
        mirror: { status: 'running', total: 3, queued: 2, running: 1, succeeded: 0, failed: 0, cancelled: 0 },
        metadata: { status: 'queued', total: 3, queued: 3, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
        projection: { status: 'queued', total: 1, queued: 1, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
      },
      work_units: [{ work_id: 'w1', title: '摇曳露营', media_type: 'tv', episode_count: 12, asset_count: 12, overall_status: 'running_mirror', mirror: { job_id: 'm', status: 'running', attempts: 1, last_error: '' }, metadata: { job_id: 'x', status: 'queued', attempts: 0, last_error: '' } }],
    },
  })
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-active')
  render(<MediaManagementPage />)

  // 默认 overview：来源卡可见、命令栏可见、未跳进导入/执行页。
  expect(await screen.findByRole('heading', { name: '媒体库' })).toBeVisible()
  expect(screen.getByRole('button', { name: '导入媒体' })).toBeVisible()
  expect(screen.getByRole('button', { name: '媒体库维护' })).toBeVisible()
  expect(screen.queryByRole('button', { name: '返回媒体管理' })).not.toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: '导入步骤' })).not.toBeInTheDocument()
  // 后台状态只更新卡片。
  expect(screen.getByText('115 动画')).toBeVisible()

  // 显式点击“查看进度”后才进入执行页。
  fireEvent.click(screen.getByRole('button', { name: '查看进度' }))
  expect(await screen.findByRole('heading', { name: '建立媒体库' })).toBeVisible()
  expect(screen.getByRole('button', { name: '返回媒体管理' })).toBeVisible()
})

test('来源卡把内容来源与导入方式分层，不重复显示内容来源', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  const firstView = render(<MediaManagementPage />)

  expect(await screen.findByText('115 动画')).toBeVisible()
  // 来源是 115 网盘，OpenList 只是扫描方式。
  expect(screen.getByText('115 网盘')).toBeVisible()
  expect(screen.getByText('OpenList 扫描')).toBeVisible()
  expect(screen.queryByText('115 网盘 · OpenList 扫描')).not.toBeInTheDocument()
  expect(screen.queryByText('OpenList 来源')).not.toBeInTheDocument()
  // 目录树基线也分层：来源 + 导入方式。
  api.sourceLibraries.mockResolvedValue({
    cards: [cardFixture({ source_mode: 'tree_openlist', ingest_method: 'directory_tree', provider: 'baidu', display_name: '百度动画' })],
  })
  // 卸载后以第二张卡独立挂载，避免两个页面同时存在导致断言误通过。
  firstView.unmount()
  render(<MediaManagementPage />)
  expect(await screen.findByText('目录树基线 · OpenList 更新')).toBeVisible()
})

test('维护入口在默认路径可见并展示孤儿历史影响', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  expect(await screen.findByRole('heading', { name: '按来源清理' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '生成删除预览' }))
  await waitFor(() => expect(api.maintenancePreview).toHaveBeenCalledWith('all'))
  // 预览包含孤儿历史/进度/追更影响
  expect(await screen.findByText(/播放历史/)).toBeVisible()
})

test('维护预览以内容来源摘要展示，不泄露内部来源根 ID', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.maintenancePreview.mockResolvedValue({
    preview_id: 'prev-readable', scope: 'all', created_at: '2026-08-25T00:00:00Z', expires_at: '2026-08-25T02:00:00Z',
    root_ids: ['root_internal_115_a', 'root_internal_115_b'], root_count: 2, work_count: 2, orphan_work_count: 1, mixed_work_count: 1, asset_count: 3, artifact_count: 2,
    artifact_summaries: ['115/动画/封面.jpg'], blocked: false, blocked_job_count: 0, blocked_job_types: [],
    history_count: 1, progress_count: 1, tracking_count: 1, warnings: [],
    root_names: [{ root_id: 'root_internal_115_a', provider: 'pan115' }, { root_id: 'root_internal_115_b', provider: 'pan115' }], digest: 'd'.repeat(64),
  })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  fireEvent.click(await screen.findByRole('button', { name: '生成删除预览' }))

  expect(await screen.findByText('115 网盘 · 2 个来源')).toBeVisible()
  expect(screen.queryByText(/root_internal_115/)).not.toBeInTheDocument()
})

test('全选预览会说明未确认来源被安全跳过，而不泄露内部根标识', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.maintenancePreview.mockResolvedValue({
    preview_id: 'prev-safe-all', scope: 'all', created_at: '2026-08-25T00:00:00Z', expires_at: '2026-08-25T02:00:00Z',
    root_ids: ['root-baidu'], root_count: 1, skipped_root_count: 1, skipped_provider_counts: [{ provider: 'pan115', count: 1 }],
    work_count: 2, orphan_work_count: 1, mixed_work_count: 1, asset_count: 3, artifact_count: 2,
    artifact_summaries: ['百度/动画/封面.jpg'], blocked: false, blocked_job_count: 0, blocked_job_types: [],
    history_count: 1, progress_count: 1, tracking_count: 1,
    warnings: ['1 个尚未确认导入的来源未纳入本次清理，它们没有可清理的媒体库数据'],
    root_names: [{ root_id: 'root-baidu', provider: 'baidu' }], digest: 'd'.repeat(64),
  })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  fireEvent.click(await screen.findByRole('button', { name: '生成删除预览' }))

  expect(await screen.findByText(/1 个未完成导入的来源已跳过/)).toBeVisible()
  expect(screen.queryByText(/root-draft|root-baidu/)).not.toBeInTheDocument()
})

test('危险清理必须经确认复选框后才能提交', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  fireEvent.click(await screen.findByRole('button', { name: '生成删除预览' }))

  const confirm = await screen.findByRole('button', { name: '确认清理' })
  expect(confirm).toBeDisabled()
  expect(api.maintenanceConfirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('checkbox', { name: '我已确认清理范围；源视频、外部 TXT 和设置不会被删除' }))
  fireEvent.click(confirm)
  await waitFor(() => expect(api.maintenanceConfirm).toHaveBeenCalled())
})

test('确认来源清理后刷新来源卡，并按实际失败状态提示', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.maintenanceConfirm.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'partial_failed',
    retired_root_count: 1, root_names: [{ root_id: 'root-115', provider: 'pan115' }],
    orphan_work_count: 1, mixed_work_count: 0, artifact_count: 1,
    artifact_results: [{ path: 'root-115/a.jpg', status: 'blocked' }], projection_status: 'ok',
  })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  fireEvent.click(await screen.findByRole('button', { name: '生成删除预览' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: '我已确认清理范围；源视频、外部 TXT 和设置不会被删除' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认清理' }))

  expect(await screen.findByText(/部分受控生成物清理失败/)).toBeVisible()
  expect(screen.queryByText(/^清理完成：/)).not.toBeInTheDocument()
  await waitFor(() => expect(api.sourceLibraries.mock.calls.length).toBeGreaterThan(1))
})


test('来源卡只保留来源摘要和 Fluent 操作按钮', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  const { container } = render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  const identity = container.querySelector('.media-v4-source-card-identity')
  const scale = container.querySelector('.media-v4-source-card-scale')
  expect(identity).not.toBeNull()
  expect(scale).not.toBeNull()
  expect(identity!.textContent).toContain('115 网盘')
  expect(identity!.textContent).toContain('OpenList 扫描')
  expect(scale!.textContent).toContain('3 部作品')
  expect(scale!.textContent).not.toContain('摇曳露营')
  expect(screen.queryByRole('list', { name: '作品预览' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '查看进度' })).toBeVisible()
  expect(screen.getByRole('button', { name: '检查更新' })).toBeVisible()
})

test('部分失败后显示重试按钮并调用 resume API', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.maintenanceConfirm.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'partial_failed',
    retired_root_count: 1, root_names: [{ root_id: 'root-baidu', provider: 'baidu' }],
    orphan_work_count: 1, mixed_work_count: 1, artifact_count: 2,
    artifact_results: [{ path: 'root-baidu/a.jpg', status: 'removed' }, { path: 'root-baidu/b.jpg', status: 'failed' }],
    projection_status: 'ok',
  })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))
  await screen.findByRole('heading', { name: '按来源清理' })
  fireEvent.click(screen.getByRole('button', { name: '生成删除预览' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: '我已确认清理范围；源视频、外部 TXT 和设置不会被删除' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认清理' }))

  expect(await screen.findByText(/部分受控生成物清理失败/)).toBeVisible()
  const retry = await screen.findByRole('button', { name: '重试未完成清理' })
  fireEvent.click(retry)
  await waitFor(() => expect(api.maintenanceResume).toHaveBeenCalledWith('prev-1'))
  expect(await screen.findByText(/清理完成/)).toBeVisible()
})


test('来源卡使用 GridView 式固定方形项目布局', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  const { container } = render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  const card = container.querySelector('.media-v4-library-source-card')
  expect(card).not.toBeNull()
  const identity = container.querySelector('.media-v4-source-card-identity')
  const scale = container.querySelector('.media-v4-source-card-scale')
  expect(identity).not.toBeNull()
  expect(scale).not.toBeNull()
  // 来源卡按等尺寸项目排列，避免宽卡把页面撑成维护表格；
  // jsdom 不计算实际尺寸，因此锁定 CSS 结构合同本身。
  const css = readFileSync(join(__dirname, '../../src/index.css'), 'utf-8')
  expect(css).toContain('grid-template-columns: repeat(auto-fill, minmax(270px, 300px));')
  expect(css).toContain('aspect-ratio: 1 / 1;')
  expect(css).not.toContain('repeat(auto-fit, minmax(540px, 1fr))')
})
