import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { V4RecognitionSummary } from '../../src/components/media/V4RecognitionSummary'
import type { V4Preview } from '../../src/api/mediaV4'

test('同一作品只显示一个修正入口，文件与具体原因在修正窗口逐项查看', () => {
  const issues = [1, 2, 3].map(n => ({ code: 'episode_number_unresolved', evidence_id: `ev${n}`, message: `具体问题${n}` }))
  const preview: V4Preview = { revision_id: 'rev', status: 'draft', works: [{ work_key: 'work', preferred_title: 'Love Live', media_type: 'tv', year: null, source_evidence_ids: ['ev1', 'ev2', 'ev3'] }], episodes: [], work_assets: [], issues }
  render(<V4RecognitionSummary preview={preview} issues={issues} evidenceEntries={[]} overrideDrafts={{}} busy={false} onOverrideChange={vi.fn()} onApplyOverride={vi.fn()} />)
  expect(screen.getAllByRole('button', { name: '修正', exact: true })).toHaveLength(1)
  expect(screen.queryByText('具体问题1')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '修正', exact: true }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('具体问题1')).toBeVisible()
  fireEvent.click(within(dialog).getByRole('button', { name: /ev3/ }))
  expect(within(dialog).getByText('具体问题3')).toBeVisible()
  expect(within(dialog).queryByText('具体问题1')).not.toBeInTheDocument()
})
