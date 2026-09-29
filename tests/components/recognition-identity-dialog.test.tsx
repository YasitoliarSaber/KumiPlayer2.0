import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { V4RecognitionSummary } from '../../src/components/media/V4RecognitionSummary'
import type { V4Preview } from '../../src/api/mediaV4'

function setup(code: string) {
  const issue = { code, evidence_id: 'ev', message: '作品身份需要重新检查' }
  const preview: V4Preview = { revision_id: 'rev', status: 'draft', works: [{ work_key: 'work', preferred_title: 'Yuru Camp', media_type: 'tv', year: null, source_evidence_ids: ['ev'] }], episodes: [], work_assets: [], issues: [issue] }
  const onOpenMaintenance = vi.fn()
  const onApplyOverride = vi.fn()
  render(<V4RecognitionSummary preview={preview} issues={preview.issues} evidenceEntries={[]} overrideDrafts={{ ev: { title: 'Yuru Camp', mediaType: 'tv', season: '1', episode: '1' } }} busy={false} onOverrideChange={vi.fn()} onApplyOverride={onApplyOverride} onOpenMaintenance={onOpenMaintenance} />)
  return { onOpenMaintenance, onApplyOverride }
}

test('身份冲突进入维护说明，不显示单集编辑，也不直接删除', () => {
  const actions = setup('work_identity_conflict')
  fireEvent.click(screen.getByRole('button', { name: '处理身份冲突' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText(/修改单集标题或集号不能解决/)).toBeVisible()
  expect(within(dialog).queryByRole('textbox', { name: '修正集号' })).not.toBeInTheDocument()
  expect(dialog).toHaveClass('media-v4-recognition-dialog')
  fireEvent.click(within(dialog).getByRole('button', { name: '打开媒体库维护' }))
  expect(actions.onOpenMaintenance).toHaveBeenCalledOnce()
  expect(actions.onApplyOverride).not.toHaveBeenCalled()
})

test('普通识别修正保持正常表单，弹窗有独立遮罩和实体居中表面', () => {
  setup('missing_episode_number')
  fireEvent.click(screen.getByRole('button', { name: '修正' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByRole('textbox', { name: '修正集号' })).toBeVisible()
  expect(within(dialog).getByText('季度')).toBeVisible()
  expect(within(dialog).getByText('集号')).toBeVisible()
  expect(dialog.querySelector('.fui-FluentProvider')).not.toBeNull()
  expect(document.querySelector('.media-v4-recognition-backdrop')).not.toBeNull()
  const css = readFileSync('src/index.css', 'utf8')
  expect(css).toMatch(/\.media-v4-recognition-dialog\s*\{[^}]*position: fixed[^}]*background:/s)
  expect(css).toMatch(/\.media-v4-recognition-dialog\s*\{[^}]*background: var\(--surface-solid\)/s)
})

test('未知类型和集号保持空白，不把缺失证据预填为第 1 集', () => {
  const issue = { code: 'episode_number_unresolved', evidence_id: 'ev', message: '集号未定' }
  const preview: V4Preview = {
    revision_id: 'rev', status: 'draft',
    works: [{ work_key: 'work', preferred_title: '天元突破', media_type: 'unknown', year: null, source_evidence_ids: ['ev'] }],
    episodes: [{ episode_key: 'episode', work_key: 'work', local_season_number: null, local_episode_number: null, absolute_episode_number: null, season_kind: 'unknown', episode_kind: 'unknown', special_number: null, edition_key: '', asset_evidence_ids: ['ev'] }],
    work_assets: [], issues: [issue],
  }
  render(<V4RecognitionSummary preview={preview} issues={preview.issues} evidenceEntries={[]} overrideDrafts={{}} busy={false} onOverrideChange={vi.fn()} onApplyOverride={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '修正' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByRole('combobox', { name: '修正媒体类型' })).toHaveValue('')
  expect(within(dialog).queryByRole('textbox', { name: '修正集号' })).not.toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: '应用修正' })).toBeDisabled()
})

test('已确认的识别只引导重扫，不能打开会失败的人工改写表单', () => {
  const issue = { code: 'episode_number_unresolved', evidence_id: 'ev', message: '集号未定' }
  const preview: V4Preview = { revision_id: 'rev', status: 'confirmed', works: [{ work_key: 'work', preferred_title: '天元突破', media_type: 'unknown', year: null, source_evidence_ids: ['ev'] }], episodes: [], work_assets: [], issues: [issue] }
  const onReimport = vi.fn()
  render(<V4RecognitionSummary preview={preview} issues={preview.issues} evidenceEntries={[]} overrideDrafts={{}} busy={false} onOverrideChange={vi.fn()} onApplyOverride={vi.fn()} onReimport={onReimport} />)
  fireEvent.click(screen.getByRole('button', { name: '查看处理方式' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).queryByRole('textbox', { name: '修正集号' })).not.toBeInTheDocument()
  fireEvent.click(within(dialog).getByRole('button', { name: '重新扫描此来源' }))
  expect(onReimport).toHaveBeenCalledOnce()
})
