import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PlayerTuningPage from '../../src/pages/PlayerTuningPage';
import { configApi } from '../../src/api/config';

vi.mock('../../src/api/config', () => ({
  configApi: {
    getConfig: vi.fn(),
    patchConfig: vi.fn(),
    openMpvConfigDir: vi.fn(),
  },
}));

vi.mock('../../src/stores/ui', () => ({
  useUiStore: (selector: (state: unknown) => unknown) => selector({
    goBack: vi.fn(),
  }),
}));

describe('PlayerTuningPage', () => {
  it('加载配置并显示当前默认值', async () => {
    (configApi.getConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      mpv_anime4k_mode: 'a',
      mpv_anime4k_quality: 'high',
    });
    render(<PlayerTuningPage />);
    await waitFor(() => {
      expect(screen.getByText('Anime4K Mode A')).toBeTruthy();
    });
    expect(screen.getByText('高质量')).toBeTruthy();
  });

  it('保存成功后写入当前默认值', async () => {
    (configApi.getConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      mpv_anime4k_mode: 'off',
      mpv_anime4k_quality: 'balanced',
    });
    (configApi.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({});
    render(<PlayerTuningPage />);
    await waitFor(() => expect(screen.getByText('关闭')).toBeTruthy());

    fireEvent.click(screen.getByText('保存默认设置'));
    await waitFor(() => {
      // B7-2 起保存会一并写回播放模式与外部路径；这里仍锁定 Anime4K 的取值。
      expect(configApi.patchConfig).toHaveBeenCalledWith(expect.objectContaining({
        mpv_anime4k_mode: 'off',
        mpv_anime4k_quality: 'balanced',
        player_mode: 'internal',
        external_mpv_path: '',
      }));
    });
  });

  it('保存失败显示错误信息', async () => {
    (configApi.getConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      mpv_anime4k_mode: 'off',
      mpv_anime4k_quality: 'balanced',
    });
    (configApi.patchConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('网络错误'));
    render(<PlayerTuningPage />);
    await waitFor(() => expect(screen.getByText('关闭')).toBeTruthy());

    fireEvent.click(screen.getByText('保存默认设置'));
    await waitFor(() => {
      expect(screen.getByText(/保存失败：网络错误/)).toBeTruthy();
    });
  });

  it('打开受限的 MPV 配置文件夹', async () => {
    (configApi.getConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      mpv_anime4k_mode: 'off',
      mpv_anime4k_quality: 'balanced',
    });
    (configApi.openMpvConfigDir as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
    render(<PlayerTuningPage />);

    await screen.findByText('关闭');
    fireEvent.click(screen.getByRole('button', { name: '打开 MPV 配置文件夹' }));

    await waitFor(() => expect(configApi.openMpvConfigDir).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('已打开 MPV 配置文件夹')).toBeTruthy();
  });

  it('在当前主题容器内展开 Anime4K 选项', async () => {
    (configApi.getConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      mpv_anime4k_mode: 'off',
      mpv_anime4k_quality: 'balanced',
    });
    const { container } = render(<PlayerTuningPage />);

    await screen.findByText('关闭');
    fireEvent.click(screen.getByRole('combobox', { name: '模式' }));

    expect(await screen.findByRole('listbox')).toBeTruthy();
    expect(container.querySelector('.player-tuning-page')).toContainElement(screen.getByRole('listbox'));
  });
});
