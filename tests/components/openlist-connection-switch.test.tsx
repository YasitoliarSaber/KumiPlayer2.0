import { act, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import OpenListFolderBrowser from '../../src/components/media/OpenListFolderBrowser'

const browse = vi.hoisted(() => vi.fn())
vi.mock('../../src/api/openlist', () => ({ openlistApi: { browse } }))

const result = (name: string) => ({
  path: '/', parent_path: null, remote_root: '/', page: 1, per_page: 100,
  total: 1, has_more: false, connection_state: 'verified',
  entries: [{ name, remote_path: `/${name}`, is_dir: true }],
  cache: { cached: false, status: 'none', refreshing: false, refresh_failed: false },
})

test('switching connection drops stale directory replies and requests the selected identity', async () => {
  let finishA!: (value: unknown) => void
  browse.mockReturnValueOnce(new Promise((resolve) => { finishA = resolve }))
    .mockResolvedValueOnce(result('连接乙目录'))
  const callbacks = { onPathChange: vi.fn(), onGoSettings: vi.fn() }
  const view = render(<OpenListFolderBrowser configured initialPath="/" connectionId="a" {...callbacks} />)
  await waitFor(() => expect(browse).toHaveBeenCalledTimes(1))
  view.rerender(<OpenListFolderBrowser configured initialPath="/" connectionId="b" {...callbacks} />)
  expect(await screen.findByRole('button', { name: '打开文件夹 连接乙目录' })).toBeInTheDocument()
  await act(async () => finishA(result('连接甲迟到目录')))
  expect(screen.queryByRole('button', { name: '打开文件夹 连接甲迟到目录' })).not.toBeInTheDocument()
  expect(browse).toHaveBeenLastCalledWith('/', 1, false, 100, 'b')
})
