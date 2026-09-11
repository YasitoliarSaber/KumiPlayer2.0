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
