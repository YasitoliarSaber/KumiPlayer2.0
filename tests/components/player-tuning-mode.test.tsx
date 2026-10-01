/** B7-2：播放模式（内置播放器 / 外部 MPV 整合包）的界面合同。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import PlayerTuningPage from '../../src/pages/PlayerTuningPage'

const config = vi.hoisted(() => ({
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  openMpvConfigDir: vi.fn(),
  testMpv: vi.fn(),
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

test('外部整合包隐藏内置 Anime4K 控件并保留其默认值', async () => {
  config.getConfig.mockResolvedValue(baseConfig({ player_mode: 'external', external_mpv_path: 'D:\\Pack\\mpv.exe' }))
  render(<PlayerTuningPage />)
  await screen.findByLabelText('整合包 MPV 可执行文件')
  expect(screen.queryByRole('combobox', { name: '模式' })).not.toBeInTheDocument()
  expect(screen.getByText(/Anime4K 由整合包管理/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '保存默认设置' }))
  await waitFor(() => expect(config.patchConfig).toHaveBeenCalledWith({ player_mode: 'external', external_mpv_path: 'D:\\Pack\\mpv.exe' }))
})

test('读取失败时不能用空白默认值覆盖已保存配置', async () => {
  config.getConfig.mockRejectedValue(new Error('暂时离线'))
  render(<PlayerTuningPage />)
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: '保存默认设置' })).toBeDisabled()
})

test('内置播放器模式不显示外部路径输入，并说明无需准备', async () => {
  config.getConfig.mockResolvedValue(baseConfig())

  render(<PlayerTuningPage />)

  expect(await screen.findByText(/不需要你准备任何东西/)).toBeVisible()
  expect(screen.queryByLabelText('整合包 MPV 可执行文件')).not.toBeInTheDocument()
})

test('检测内置播放器时使用内置运行时入口，不检查空白外部路径', async () => {
  config.getConfig.mockResolvedValue(baseConfig())
  config.testMpv.mockResolvedValue({ ok: true, message: '内置播放器已就绪' })
  render(<PlayerTuningPage />)
  fireEvent.click(await screen.findByRole('button', { name: '检测播放器' }))
  await waitFor(() => expect(config.testMpv).toHaveBeenCalledWith())
  expect(await screen.findByText('内置播放器已就绪')).toBeVisible()
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

test('检测整合包目录后采用正规路径，保存前不能打开错误的配置目录', async () => {
  config.getConfig.mockResolvedValue(baseConfig())
  config.testMpv.mockResolvedValue({ ok: true, message: '播放器可用', version: 'mpv 0.41', executable_path: 'D:\\Pack\\mpv.exe' })
  config.patchConfig.mockResolvedValue(baseConfig({ player_mode: 'external', external_mpv_path: 'D:\\Pack\\mpv.exe' }))
  render(<PlayerTuningPage />)
  fireEvent.click(await screen.findByRole('radio', { name: '外部 MPV 整合包' }))
  fireEvent.change(screen.getByLabelText('整合包 MPV 可执行文件'), { target: { value: 'D:\\Pack' } })
  expect(screen.getByRole('button', { name: '打开 MPV 配置文件夹' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '检测播放器' }))
  await waitFor(() => expect(config.testMpv).toHaveBeenCalledWith('D:\\Pack', 'external'))
  await waitFor(() => expect(screen.getByLabelText('整合包 MPV 可执行文件')).toHaveValue('D:\\Pack\\mpv.exe'))
  expect(screen.getByText(/播放器可用/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '保存默认设置' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '打开 MPV 配置文件夹' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '打开 MPV 配置文件夹' }))
  await waitFor(() => expect(config.openMpvConfigDir).toHaveBeenCalledWith('external'))
})

test('配置加载失败后重试恢复已有设置', async () => {
  config.getConfig.mockRejectedValueOnce(new Error('暂时离线')).mockResolvedValueOnce(baseConfig({ mpv_anime4k_mode: 'b' }))
  render(<PlayerTuningPage />)
  fireEvent.click(await screen.findByRole('button', { name: '重新读取' }))
  await waitFor(() => expect(screen.getByRole('combobox', { name: '模式' })).toHaveValue('b'))
  expect(screen.getByRole('button', { name: '保存默认设置' })).toBeEnabled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
