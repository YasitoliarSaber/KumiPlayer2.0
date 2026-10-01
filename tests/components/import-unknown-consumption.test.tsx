import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import { playbackApi } from '../../src/api/playback';
import { useLibraryStore } from '../../src/stores/library';
import { useUiStore } from '../../src/stores/ui';
import WorkDetailPage from '../../src/pages/WorkDetailPage';

test('unknown 视频保留未分季并可播放，不显示 S00E00', async () => {
  const work = {
    work_id: 'unknown-work', title: '未定位作品', media_type: 'unknown', show_type: '',
    metadata_source: 'retained', refresh_status: 'failed', episode_mapping_status: 'partial', mapped_count: 1, total_count: 2,
    genres: [], studios: [], tags: [], sources: ['local'], source: 'local',
    seasons: [{ season_id: 'unassigned-a', season_number: null, group_type: 'unassigned', label: '未分季', episode_count: 1 },
      { season_id: 'unassigned-b', season_number: null, group_type: 'unassigned', label: '另一未分季', episode_count: 1 }],
    episodes: [{ episode_id: 'unknown-episode', season_id: 'unassigned-a', season_number: null,
      episode_number: null, title: '未知内容甲', group_type: 'unassigned', kind: 'unknown',
      asset_id: 'unknown-asset', source: 'local', availability: 'available',
      assets: [{ asset_id: 'unknown-asset', source: 'local', availability: 'available', playback_locator: 'Z:/fixture/a.mkv' }] },
      { episode_id: 'unknown-b', season_id: 'unassigned-b', season_number: null, episode_number: null,
        title: '未知内容乙', group_type: 'unassigned', kind: 'unknown', asset_id: 'asset-b', source: 'local', availability: 'available' }],
  };
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: vi.fn() });
  useUiStore.setState({ page: 'detail', selectedWorkId: work.work_id, selectedSeasonNumber: null });
  useLibraryStore.setState({ works: [work as never], history: [], getWorkDetail: vi.fn().mockResolvedValue(work), updateWorkWatchStatus: vi.fn() });
  vi.spyOn(playbackApi, 'getProgress').mockResolvedValue({ items: [] });
  vi.spyOn(playbackApi, 'getHistory').mockResolvedValue({ items: [] });
  vi.spyOn(playbackApi, 'getStatus').mockResolvedValue({ status: 'idle', session: null });
  const play = vi.spyOn(playbackApi, 'play').mockResolvedValue({ ok: true, session_id: 'unknown-session' } as never);
  const { container } = render(<WorkDetailPage />);
  await screen.findAllByText('未知内容甲');
  expect(container.textContent).not.toContain('S00E00');
  expect(container.textContent).toContain('未分季');
  expect(container.textContent).toContain('资料已保留，刷新失败');
  // 详情页的映射进度文案已由「剧集资料」改为「分集资料」（提交 1e5e6d4）。
  expect(container.textContent).toContain('分集资料 1/2');
  expect(container.querySelector('[data-episode-id="unknown-b"]')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '播放第 ? 集：未知内容甲' }));
  await waitFor(() => expect(play).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('combobox'));
  fireEvent.click(await screen.findByRole('option', { name: '另一未分季' }));
  await waitFor(() => expect(container.querySelector('[data-episode-id="unknown-b"]')).not.toBeNull());
  expect(container.querySelector('[data-episode-id="unknown-episode"]')).toBeNull();
});
