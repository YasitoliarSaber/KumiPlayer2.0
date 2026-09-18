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
  sourceLibraryDeletionPreview: vi.fn(),
  deleteSourceLibrary: vi.fn(),
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

test('来源卡将关联提示与真实待处理分开，操作按用途分组', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture({ relation_pending_count: 2 })] })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')
  expect(screen.queryByText(/个待处理事项/)).not.toBeInTheDocument()
  expect(screen.getByText('2 项关联信息待补全，不影响入库和播放')).toBeVisible()
  expect(screen.getByRole('group', { name: '导入与更新' })).toContainElement(screen.getByRole('button', { name: '检查更新' }))
  expect(screen.getByRole('group', { name: '来源卡管理' })).toContainElement(screen.getByRole('button', { name: '重命名来源卡：115 动画' }))
})

test('按来源删除媒体库：先展示预览，再确认，且预览态不暴露「移除」', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.sourceLibraryDeletionPreview.mockResolvedValue({
    root_id: 'root-115', works_total: 3, works_removable: 2, works_shared: 1,
    artifacts_total: 24, artifact_files: 20, artifact_bytes: 3 * 1048576,
    files_outside_mirror_count: 0,
    removable_samples: ['摇曳露营', '孤独摇滚'], shared_samples: ['摇曳露营 剧场版'], blockers: [],
  })
  api.deleteSourceLibrary.mockResolvedValue({ root_id: 'root-115', job_id: 'job-delete', status: 'queued' })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))
  fireEvent.click(await screen.findByRole('button', { name: '同时删除媒体库…' }))

  // 预览必须先把影响范围说清楚（含"被其他来源共享会保留"）。
  expect(await screen.findByText(/2/)).toBeVisible()
  expect(await screen.findByText(/部作品离开媒体库/)).toBeVisible()
  expect(screen.getByText(/被其他来源共享，会保留/)).toBeVisible()
  expect(api.sourceLibraryDeletionPreview).toHaveBeenCalledWith('root-115')
  // 预览态下隐藏「移除」，避免用户以为在删除却点到较弱的隐藏操作。
  expect(screen.queryByRole('button', { name: '移除' })).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: '确认删除媒体库' }))

  await waitFor(() => expect(api.deleteSourceLibrary).toHaveBeenCalledWith('root-115'))
  expect(await screen.findByText(/已开始按来源删除/)).toBeVisible()
})

test('按来源删除被阻断时给出原因，且不提供确认删除', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.sourceLibraryDeletionPreview.mockResolvedValue({
    root_id: 'root-115', works_total: 3, works_removable: 2, works_shared: 1,
    artifacts_total: 24, artifact_files: 20, artifact_bytes: 0,
    files_outside_mirror_count: 0,
    removable_samples: [], shared_samples: [], blockers: ['该来源仍有进行中的扫描，请先取消或等待结束'],
  })
  render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))
  fireEvent.click(await screen.findByRole('button', { name: '同时删除媒体库…' }))

  expect(await screen.findByText(/暂时无法删除：该来源仍有进行中的扫描/)).toBeVisible()
  expect(screen.queryByRole('button', { name: '确认删除媒体库' })).toBeNull()
  expect(api.deleteSourceLibrary).not.toHaveBeenCalled()
})

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

  expect(await screen.findByRole('dialog', { name: '移除来源卡？' })).toBeVisible()
  expect(screen.getByRole('dialog', { name: '移除来源卡？' }).className).toContain('media-v4-source-card-delete-dialog')
  expect(screen.getByRole('dialog', { name: '移除来源卡？' }).querySelector('.media-v4-source-card-dialog-actions')).toBeInTheDocument()
  expect(screen.getByText(/不会删除媒体库、镜像、资料或观看状态/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '移除', exact: true }))

  await waitFor(() => expect(api.hideSourceLibraryCard).toHaveBeenCalledWith('root-115'))
  expect(screen.queryByText('115 动画')).not.toBeInTheDocument()
})

test('来源卡列表过期时，删除接口的 404 按幂等成功处理', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.hideSourceLibraryCard.mockRejectedValueOnce(new ApiError(404, '来源卡不存在或已移除'))
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))
  fireEvent.click(await screen.findByRole('button', { name: '移除', exact: true }))

  await waitFor(() => expect(screen.queryByText('115 动画')).not.toBeInTheDocument())
  expect(screen.queryByText('来源卡不存在或已移除')).not.toBeInTheDocument()
})

test('来源卡可以在不影响来源路径的情况下重命名', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '重命名来源卡：115 动画' }))

  expect(await screen.findByRole('dialog', { name: '重命名来源卡' })).toBeVisible()
  expect(screen.getByText('来源名称')).toBeVisible()
  expect(screen.getByText('仅修改来源卡显示名，不会改动实际目录。')).toBeVisible()
  expect(screen.getByRole('dialog', { name: '重命名来源卡' }).querySelector('.media-v4-source-card-dialog-actions')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('来源名称'), { target: { value: '我的动画库' } })
  fireEvent.click(screen.getByRole('button', { name: '保存名称' }))

  await waitFor(() => expect(api.renameSourceLibraryCard).toHaveBeenCalledWith('root-115', '我的动画库'))
  expect(screen.getByText('我的动画库')).toBeVisible()
})

test('来源卡列表过期时，重命名接口的 404 会移除失效卡片', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  api.renameSourceLibraryCard.mockRejectedValueOnce(new ApiError(404, '来源卡不存在或已移除'))
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '重命名来源卡：115 动画' }))
  fireEvent.click(await screen.findByRole('button', { name: '保存名称' }))

  await waitFor(() => expect(screen.queryByText('115 动画')).not.toBeInTheDocument())
  expect(screen.queryByRole('dialog', { name: '重命名来源卡' })).not.toBeInTheDocument()
})

test('删除来源卡确认框在 Portal 中保留 Fluent 主题容器', async () => {
  api.sourceLibraries.mockResolvedValue({ cards: [cardFixture()] })
  render(<MediaManagementPage />)

  await screen.findByText('115 动画')
  fireEvent.click(screen.getByRole('button', { name: '删除来源卡：115 动画' }))

  const dialog = await screen.findByRole('dialog', { name: '移除来源卡？' })
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

test('预算暂停的来源卡提供继续扫描并复用同一 scan_id', async () => {
  api.sourceLibraries.mockResolvedValue({
    cards: [cardFixture({
      last_scan_mode: 'full',
      overall_status: 'needs_attention',
      phase: 'scan',
      scan: {
        scan_id: 'scan-paused', status: 'paused', stage: 'paused',
        stage_label: '本次巡检已达请求预算，可继续扫描',
        processed_count: 400, total_count: 400, progress: 1,
        resumable: true, heartbeat_at: new Date().toISOString(), cancel_requested: 0,
      },
      active_task: null,
      attention_count: 1,
      available_actions: ['inspect', 'resume'],
      can_resume: true,
    })],
  })
  api.startDurableScan.mockResolvedValue({
    scan_id: 'scan-paused', root_id: 'root-115', scan_mode: 'full', status: 'running',
  })
  api.durableScan.mockResolvedValue({
    scan_id: 'scan-paused', root_id: 'root-115', status: 'completed', stage: 'ready',
    stage_label: '识别结果已就绪', evidence_count: 400, processed_count: 400,
    total_count: 400, error: '', entries: [],
  })
  api.drafts.mockResolvedValue({ drafts: [] })

  render(<MediaManagementPage />)
  await screen.findByText('115 动画')

  fireEvent.click(screen.getByRole('button', { name: '继续扫描' }))

  // 必须复用同一 scan_id：换新 id 会从根目录重扫，已花的请求全部作废。
  await waitFor(() => expect(api.startDurableScan).toHaveBeenCalledWith(
    expect.objectContaining({ resume_scan_id: 'scan-paused', scan_mode: 'full' }),
  ))
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
