import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { playbackApi } from '../../src/api/playback';
import { useLibraryStore } from '../../src/stores/library';
import { useUiStore } from '../../src/stores/ui';
import WorkDetailPage from '../../src/pages/WorkDetailPage';

const work = {
  work_id: 'work-v4',
  title: '测试动画',
  original_title: 'Test Anime',
  year: 2026,
  rating: 8.7,
  plot: '用于验证 V4 详情页的作品简介。',
  genres: ['动画', '冒险'],
  studios: ['Kumi Studio'],
  media_type: 'tv',
  show_type: 'anime_series',
  source: 'baidu',
  sources: ['baidu', 'pan115'],
  card_type: 'main_series',
  poster_path: '',
  fanart_path: '',
  local_poster_path: '',
  local_fanart_path: '',
  clearlogo_path: '',
  dir_path: '',
  tags: [],
  related_works: [],
  last_played: null,
  watch_status: { work_id: 'work-v4', status: '', note: '', favorite: false, updated_at: '' },
  seasons: [
    { season_id: 'season-1', season_number: 1, group_type: 'season', label: '第 1 季', episode_count: 1 },
  ],
  episodes: [
    {
      episode_id: 'episode-1',
      season_number: 1,
      episode_number: 1,
      title: '启程',
      group_type: 'season',
      kind: 'episode',
      source: 'baidu',
      asset_id: 'asset-primary',
      assets: [
        { asset_id: 'asset-primary', source: 'baidu', availability: 'available', playback_locator: 'K:/百度网盘/测试动画/01.mkv' },
        { asset_id: 'asset-alt', source: 'pan115', availability: 'available', playback_locator: 'K:/115网盘/测试动画/01.mkv' },
      ],
    },
  ],
};

beforeEach(() => {
  useUiStore.setState({
    page: 'detail',
    selectedWorkId: work.work_id,
    activeCategory: 'anime_series',
    selectedSeasonNumber: 1,
  });
  useLibraryStore.setState({
    works: [work as never],
    history: [],
    getWorkDetail: vi.fn().mockResolvedValue(work),
    updateWorkWatchStatus: vi.fn(),
  });
  vi.spyOn(playbackApi, 'getProgress').mockResolvedValue({ items: [] });
  vi.spyOn(playbackApi, 'getHistory').mockResolvedValue({ items: [] });
  vi.spyOn(playbackApi, 'getStatus').mockResolvedValue({ status: 'idle', session: null });
});

test('按旧版沉浸式结构展示 V4 季度和剧集信息', async () => {
  const { container } = render(<WorkDetailPage />);

  expect(await screen.findByRole('heading', { name: '测试动画' })).toBeVisible();
  expect(container.querySelector('.detail-page.detail-classic-page')).not.toBeNull();
  expect(container.querySelector('.detail-hero')).not.toBeNull();
  expect(container.querySelector('.detail-content-drawer')).not.toBeNull();
  expect(screen.getByRole('combobox', { name: '选择季度' })).toBeVisible();
  expect(screen.getByText('启程')).toBeVisible();
});

test('播放剧集时沿用 V4 Work、Episode 和首选 Asset 身份', async () => {
  const play = vi.spyOn(playbackApi, 'play').mockResolvedValue({ ok: true, session_id: 'session-1' } as never);
  render(<WorkDetailPage />);

  fireEvent.click(await screen.findByRole('button', { name: /播放第 1 集：启程/ }));

  await waitFor(() => expect(play).toHaveBeenCalledWith({
    work_id: 'work-v4',
    episode_id: 'episode-1',
    asset_id: 'asset-primary',
  }));
});

test('更多菜单只显示已经接通 V4 的操作', async () => {
  render(<WorkDetailPage />);

  fireEvent.click(await screen.findByRole('button', { name: '更多操作' }));

  expect(screen.getByRole('menuitem', { name: '打开视频文件夹' })).toBeVisible();
  expect(screen.queryByRole('menuitem', { name: '手动刮削' })).toBeNull();
  expect(screen.queryByRole('menuitem', { name: '删除该作品' })).toBeNull();
  expect(screen.queryByRole('menuitem', { name: '打开镜像文件夹' })).toBeNull();
});
