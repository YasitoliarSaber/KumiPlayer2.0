/** P-005 媒体管理首页层级与媒体库维护入口测试。 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import MediaManagementPage from '../../src/pages/MediaManagementPage'
import { useUiStore } from '../../src/stores/ui'
import { ApiError } from '../../src/api/client'

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
  hideSourceLibraryCard: vi.fn(),
  renameSourceLibraryCard: vi.fn(),
  maintenancePreview: vi.fn(),
  maintenanceConfirm: vi.fn(),
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
  api.workExecutionDetail.mockResolvedValue({ has_detail: false })
  useUiStore.setState({ page: 'manage', manageView: 'overview', navigationHistory: [], forwardHistory: [], canGoBack: false, canGoForward: false, query: '' })
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] })
  api.preview.mockResolvedValue({ revision_id: 'rev', status: 'draft', works: [], episodes: [], work_assets: [], issues: [] })
  api.status.mockResolvedValue({ revision_id: 'rev', status: 'confirmed', jobs: [] })
  api.sourceLibraries.mockResolvedValue({ cards: [] })
  api.drafts.mockResolvedValue({ drafts: [] })
  api.hideSourceLibraryCard.mockResolvedValue({ root_id: 'root-115', hidden: true })
  api.renameSourceLibraryCard.mockResolvedValue({ root_id: 'root-115', display_name: '我的动画库' })
  api.openlistStatus.mockResolvedValue({ root_id: 'r', remote_root: '/', source_mode: '', last_scan_mode: '', has_confirmed_baseline: false })
  api.maintenancePreview.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', expires_at: '2026-08-25T02:00:00Z',
    root_count: 1, work_count: 2, orphan_work_count: 1, mixed_work_count: 1, asset_count: 3, artifact_count: 2,
    artifact_summaries: ['root-baidu/a.jpg'], blocked: false, blocked_job_count: 0, blocked_job_types: [],
    warnings: ['源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、配置与凭据始终保留'],
    root_names: [{ root_id: 'root-baidu', provider: 'baidu' }], digest: 'd'.repeat(64),
  })
  api.maintenanceConfirm.mockResolvedValue({
    preview_id: 'prev-1', scope: 'all', status: 'completed',
    retired_root_count: 1, root_names: [{ root_id: 'root-baidu', provider: 'baidu' }],
    orphan_work_count: 1, mixed_work_count: 1, artifact_count: 1,
    artifact_results: [{ path: 'root-baidu/a.jpg', status: 'removed' }], projection_status: 'ok',
  })
  config.getConfig.mockResolvedValue({
    pan115_root: 'K:\\115网盘', baidu_root: 'K:\\百度网盘', local_root: 'D:\\Media',
    openlist_configured: true, openlist_remote_root: '/', openlist_mount_root: 'K:\\', openlist_routes: [],
  })
  openlist.getRoutes.mockResolvedValue({ routes: [] })
  tasks.retry.mockResolvedValue({ status: 'pending' })
})

afterEach(() => {
  cleanup()
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

test('空媒体库只保留命令栏中的一个导入主按钮', async () => {
  render(<MediaManagementPage />)

  expect(await screen.findByText('还没有导入任何媒体库')).toBeVisible()
  expect(screen.getAllByRole('button', { name: '导入媒体' })).toHaveLength(1)
  expect(screen.queryByRole('navigation', { name: '导入步骤' })).not.toBeInTheDocument()
})

test('来源卡首次短暂断连后自动重试，并移除过期连接错误', async () => {
  api.sourceLibraries
    .mockRejectedValueOnce(new ApiError(503, '无法连接 KumiPlayer 后端，请重试或使用恢复界面重启后端'))
    .mockResolvedValueOnce({ cards: [cardFixture()] })

  render(<MediaManagementPage />)

  expect(await screen.findByText('115 动画')).toBeVisible()
  expect(api.sourceLibraries).toHaveBeenCalledTimes(2)
  expect(screen.queryByText('无法连接 KumiPlayer 后端，请重试或使用恢复界面重启后端')).not.toBeInTheDocument()
})

test('本地导入创建耐久扫描任务而不等待同步扫描响应', async () => {
  api.startDurableScan.mockResolvedValue({ scan_id: 'scan-local-1', root_id: 'root-local', scan_mode: 'local', status: 'running' })
  api.durableScan.mockResolvedValue({
    scan_id: 'scan-local-1', root_id: 'root-local', status: 'completed', error: '', entries: [],
  })
  render(<MediaManagementPage />)

  await screen.findByRole('heading', { name: '媒体库' })
  fireEvent.click(screen.getByRole('button', { name: '导入媒体' }))
  fireEvent.click(await screen.findByRole('button', { name: '扫描并识别' }))

  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(expect.objectContaining({ source: 'local' })))
  expect(api.scan).not.toHaveBeenCalled()
  // 等待本用例启动的异步扫描完整收口，避免其后续导航污染下一用例。
  expect(await screen.findByRole('heading', { name: '检查识别结果' }, { timeout: 2500 })).toBeVisible()
})

test('来源卡只显示来源摘要与操作，不展示具体作品名', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  expect(await screen.findByText('115 动画')).toBeVisible()
  // 内容来源与导入方式严格分层：来源是 115 网盘，OpenList 只是扫描方式。
  expect(screen.getByText('115 网盘')).toBeVisible()
  expect(screen.getByText('OpenList 扫描')).toBeVisible()
  expect(screen.queryByText('OpenList 来源')).not.toBeInTheDocument()
  expect(screen.queryByText(/添加于 2026-08-20/)).not.toBeInTheDocument()
  expect(screen.queryByText(/最近更新 2026-08-24/)).not.toBeInTheDocument()
  // 卡片只承担来源管理，不重复展示作品库内容。
  expect(screen.queryByText('摇曳露营')).not.toBeInTheDocument()
  expect(screen.queryByText('孤独摇滚')).not.toBeInTheDocument()
  expect(screen.queryByRole('list', { name: '作品预览' })).not.toBeInTheDocument()
  expect(screen.getByText('3 部作品')).toBeVisible()
  // 用户级进度而非 raw 任务数。
  expect(screen.getByText('上次导入已处理完毕')).toBeVisible()
  expect(screen.queryByText(/6 个任务/)).not.toBeInTheDocument()
})

test('已完成来源卡可以移除卡片入口，不触碰媒体库数据', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))

  expect(await screen.findByRole('dialog', { name: '删除来源卡' })).toBeVisible()
  expect(screen.getByRole('dialog', { name: '删除来源卡' }).className).toContain('media-v4-source-card-delete-dialog')
  expect(screen.getByText(/不会删除媒体库、镜像、资料或观看状态/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '删除来源卡', exact: true }))

  await waitFor(() => expect(api.hideSourceLibraryCard).toHaveBeenCalledWith('root-115'))
  expect(screen.queryByText('115 动画')).not.toBeInTheDocument()
})

test('来源卡可以在不影响来源路径的情况下重命名', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '重命名来源卡：115 动画' }))

  expect(await screen.findByRole('dialog', { name: '重命名来源卡' })).toBeVisible()
  fireEvent.change(screen.getByLabelText('来源名称'), { target: { value: '我的动画库' } })
  fireEvent.click(screen.getByRole('button', { name: '保存名称' }))

  await waitFor(() => expect(api.renameSourceLibraryCard).toHaveBeenCalledWith('root-115', '我的动画库'))
  expect(screen.getByText('我的动画库')).toBeVisible()
})

test('删除来源卡确认框在 Portal 中保留 Fluent 主题容器', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))

  const dialog = await screen.findByRole('dialog', { name: '删除来源卡' })
  expect(dialog.querySelector('.media-v4-source-card-delete-dialog-provider')).toBeInTheDocument()
})

test('新导入不会恢复到保存的上次执行步骤', async () => {
  localStorage.setItem('kumiplayer.media-v4.active-revision', 'rev-existing')
  api.status.mockResolvedValue({ revision_id: 'rev-existing', status: 'confirmed', jobs: [], progress: null })
  render(<MediaManagementPage />)

  await screen.findByRole('heading', { name: '媒体库' })
  fireEvent.click(screen.getAllByRole('button', { name: '导入媒体' })[0])

  expect(await screen.findByRole('button', { name: '本地目录' })).toBeVisible()
  expect(screen.getByRole('navigation', { name: '导入步骤' }).querySelector('li[aria-current="step"]')?.textContent).toContain('选择来源')
})

test('媒体库维护入口生成预览并分组展示', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))

  expect(await screen.findByRole('heading', { name: '按来源清理' })).toBeVisible()
  expect(screen.getByRole('radio', { name: /^全部来源/ })).toBeChecked()
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
  const confirm = await screen.findByRole('button', { name: '确认清理' })
  expect(screen.queryByRole('checkbox', { name: /我已确认清理范围/ })).not.toBeInTheDocument()
  expect(confirm).toBeEnabled()
  fireEvent.click(confirm)

  await waitFor(() => expect(api.maintenanceConfirm).toHaveBeenCalledWith({
    preview_id: 'prev-1', scope: 'all', digest: 'd'.repeat(64),
  }))
  expect(await screen.findByText(/清理完成/)).toBeVisible()
  expect(screen.getByText(/已删除 · root-baidu\/a\.jpg/)).toBeVisible()
})

test('维护页的预览和返回操作使用带边框的标准按钮', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '媒体库维护' }))

  expect(await screen.findByRole('button', { name: '返回媒体管理' })).toHaveClass('media-v4-header-back-button')
  expect(screen.getByRole('button', { name: '生成删除预览' })).toHaveClass('media-v4-maintenance-preview-button')
})

test('失联扫描任务显示中断文案且不渲染转圈，仍可重新扫描', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [cardFixture({
      overall_status: 'needs_attention',
      phase: 'scan',
      scan: {
        scan_id: 'scan-zombie', status: 'running', stage: 'reading_source',
        stage_label: '上次扫描意外中断，请重新扫描', processed_count: 0, total_count: 0,
        progress: null, heartbeat_at: '2026-09-04T00:00:00Z', cancel_requested: 0,
      },
      active_task: null,
      progress: {
        state: 'needs_attention', stage: 'scan', current_work_id: '', current_work_title: '',
        completed_work_count: 0, total_work_count: 0, percent: null,
        message: '上次扫描意外中断，请重新扫描',
      },
      attention_count: 1,
      available_actions: ['inspect', 'resume'],
      can_resume: true,
    })],
  })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  expect(screen.getByText('上次扫描意外中断，请重新扫描')).toBeVisible()
  expect(screen.getByText('需要处理')).toBeVisible()
  expect(screen.queryByRole('button', { name: '终止任务' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重新扫描' })).toBeVisible()
  expect(screen.getByRole('button', { name: '删除来源卡：115 动画' })).toBeVisible()
})

test('排队中的扫描任务提供可用的终止按钮', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [cardFixture({
      overall_status: 'queued',
      phase: 'scan',
      scan: {
        scan_id: 'scan-queued', status: 'queued', stage: 'queued',
        stage_label: '准备读取媒体来源', processed_count: 0, total_count: 0,
        progress: null, heartbeat_at: new Date().toISOString(), cancel_requested: 0,
      },
      active_task: {
        kind: 'scan', revision_id: '', status: 'queued', stage: 'queued',
        label: '准备读取媒体来源', percent: null, can_cancel: true, cancel_requested: false,
      },
      available_actions: ['inspect', 'resume'],
      can_resume: true,
    })],
  })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  const terminate = screen.getByRole('button', { name: '终止任务' })
  expect(terminate).toBeEnabled()
})
