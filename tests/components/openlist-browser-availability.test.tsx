import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import OpenListFolderBrowser from '../../src/components/media/OpenListFolderBrowser'

const browse = vi.hoisted(() => vi.fn())
vi.mock('../../src/api/openlist', () => ({ openlistApi: { browse } }))
const result = (cached = false) => ({
  path: '/Quark', parent_path: '/', remote_root: '/', page: 1, per_page: 100,
  total: 1, has_more: false,
  entries: [{ name: '动画', remote_path: '/Quark/Anime', is_dir: true }],
  cache: { cached, status: cached ? 'fresh' : 'none', refreshing: false, refresh_failed: false },
})
beforeEach(() => browse.mockReset())

test('cached directories are not presented as currently connected', async () => {
  browse.mockResolvedValue(result(true))
  render(<OpenListFolderBrowser configured initialPath="/Quark" onPathChange={vi.fn()} onGoSettings={vi.fn()} />)
  await waitFor(() => expect(browse).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.queryByRole('button', { name: '打开文件夹 动画' })).not.toBeInTheDocument())
  expect(await screen.findByText(/连接未验证/)).toBeInTheDocument()
  expect(screen.queryByText(/缓存有效/)).not.toBeInTheDocument()
})

test('failed refresh removes the previous directory list and does not retry automatically', async () => {
  browse.mockResolvedValueOnce(result()).mockRejectedValueOnce(new Error('当前无法连接网盘'))
  render(<OpenListFolderBrowser configured initialPath="/Quark" onPathChange={vi.fn()} onGoSettings={vi.fn()} />)
  expect(await screen.findByRole('button', { name: '打开文件夹 动画' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '刷新当前层' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('当前无法连接网盘')
  expect(screen.queryByRole('button', { name: '打开文件夹 动画' })).not.toBeInTheDocument()
  expect(browse).toHaveBeenCalledTimes(2)
})

test('OpenList server response without upstream verification cannot enable scanning', async () => {
  const availability = vi.fn()
  browse.mockResolvedValue({ ...result(), connection_state: 'unverified' })
  render(<OpenListFolderBrowser configured initialPath="/Quark" onPathChange={vi.fn()} onAvailabilityChange={availability} onGoSettings={vi.fn()} />)
  expect(await screen.findByText(/连接未验证/)).toBeInTheDocument()
  expect(availability).toHaveBeenLastCalledWith(false)
  expect(screen.queryByRole('button', { name: '打开文件夹 动画' })).not.toBeInTheDocument()
})

test('explicit folder navigation requests only that current page with upstream verification', async () => {
  browse.mockResolvedValue(result())
  render(<OpenListFolderBrowser configured initialPath="/Quark" onPathChange={vi.fn()} onGoSettings={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 动画' }))
  await waitFor(() => expect(browse).toHaveBeenLastCalledWith('/Quark/Anime', 1, true, 100))
  expect(browse).toHaveBeenCalledTimes(2)
})
