import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PlayerTuningPage from '../../src/pages/PlayerTuningPage';
import { configApi } from '../../src/api/config';

vi.mock('../../src/api/config', () => ({
  configApi: {
    getConfig: vi.fn(),
    patchConfig: vi.fn(),
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
      expect(configApi.patchConfig).toHaveBeenCalledWith({
        mpv_anime4k_mode: 'off',
        mpv_anime4k_quality: 'balanced',
      });
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
});
