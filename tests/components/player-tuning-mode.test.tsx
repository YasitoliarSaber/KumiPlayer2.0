/** B7-2：播放模式（内置播放器 / 外部 MPV 整合包）的界面合同。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import PlayerTuningPage from '../../src/pages/PlayerTuningPage'

const config = vi.hoisted(() => ({
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  openMpvConfigDir: vi.fn(),
}))
const picker = vi.hoisted(() => ({ pickFile: vi.fn(), pickFolder: vi.fn() }))

vi.mock('../../src/api/config', () => ({ configApi: config }))
vi.mock('../../src/platform/folderPicker', () => picker)

function baseConfig(overrides: Record<string, unknown> = {}) {
  return {
    mpv_path: '',
    player_mode: 'internal',
    external_mpv_path: '',
    mpv_anime4k_mode: 'off',
    mpv_anime4k_quality: 'balanced',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  config.patchConfig.mockResolvedValue(baseConfig())
  config.openMpvConfigDir.mockResolvedValue(undefined)
})

test('内置播放器模式不显示外部路径输入，并说明无需准备', async () => {
  config.getConfig.mockResolvedValue(baseConfig())

  render(<PlayerTuningPage />)

  expect(await screen.findByText(/不需要你准备任何东西/)).toBeVisible()
  expect(screen.queryByLabelText('整合包 MPV 可执行文件')).not.toBeInTheDocument()
})

test('外部整合包模式保存播放模式与路径，并承诺不修改整合包目录', async () => {
  config.getConfig.mockResolvedValue(baseConfig({
    player_mode: 'external',
    external_mpv_path: 'D:\\Pack\\MPVlite\\mpv\\mpv.exe',
  }))

  render(<PlayerTuningPage />)

  expect(await screen.findByText(/不会修改该目录中的配置或脚本/)).toBeVisible()
  const input = screen.getByLabelText('整合包 MPV 可执行文件')
  expect(input).toHaveValue('D:\\Pack\\MPVlite\\mpv\\mpv.exe')

  fireEvent.click(screen.getByRole('button', { name: '保存默认设置' }))

  await waitFor(() => expect(config.patchConfig).toHaveBeenCalledWith(expect.objectContaining({
    player_mode: 'external',
    external_mpv_path: 'D:\\Pack\\MPVlite\\mpv\\mpv.exe',
  })))
})

test('可以通过文件选择器指定整合包 mpv.exe', async () => {
  config.getConfig.mockResolvedValue(baseConfig({ player_mode: 'external' }))
  picker.pickFile.mockResolvedValue('D:\\Other\\mpv\\mpv.exe')

  render(<PlayerTuningPage />)

  fireEvent.click(await screen.findByRole('button', { name: '选择' }))

  await waitFor(() => expect(screen.getByLabelText('整合包 MPV 可执行文件')).toHaveValue('D:\\Other\\mpv\\mpv.exe'))
})
