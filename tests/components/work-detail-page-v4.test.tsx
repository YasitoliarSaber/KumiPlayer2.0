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
  local_poster_path: 'D:/mirror/测试动画/poster.jpg',
  local_fanart_path: 'D:/mirror/测试动画/fanart.jpg',
  clearlogo_path: 'https://image.tmdb.org/t/p/original/logo.png',
  local_clearlogo_path: 'D:/mirror/测试动画/clearlogo.png',
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
      thumb_path: 'https://image.tmdb.org/t/p/w300/still.jpg',
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
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
    configurable: true,
    value: vi.fn(),
  });
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

  expect(await screen.findByAltText('测试动画 logo')).toBeVisible();
  expect(container.querySelector('.detail-page.detail-classic-page')).not.toBeNull();
  expect(container.querySelector('.detail-hero')).not.toBeNull();
  expect(container.querySelector('.detail-content-drawer')).not.toBeNull();
  expect(container.querySelector('.detail-hero-logo')).not.toBeNull();
  expect(container.querySelector('.detail-episode-grid.thumbnail-strip')).not.toBeNull();
  expect(screen.getByRole('combobox', { name: '选择季度' })).toBeVisible();
  expect(screen.getByText('启程')).toBeVisible();
});

test('特别篇使用 SP 编号并显示彼此可区分的本地标题', async () => {
  const specialWork = {
    ...work,
    seasons: [
      { season_id: 'season-special', season_number: 0, group_type: 'special', label: '特别篇', episode_count: 2 },
    ],
    episodes: [
      {
        ...work.episodes[0],
        episode_id: 'special-1',
        season_number: 0,
        episode_number: null,
        special_number: 1,
        title: 'SP01 - 露营小剧场',
        group_type: 'special',
        kind: 'special',
      },
      {
        ...work.episodes[0],
        episode_id: 'special-2',
        season_number: 0,
        episode_number: null,
        special_number: 2,
        title: 'SP02 - 温泉小剧场',
        group_type: 'special',
        kind: 'special',
      },
    ],
  };
  useUiStore.setState({ selectedSeasonNumber: 0 });
  useLibraryStore.setState({
    works: [specialWork as never],
    getWorkDetail: vi.fn().mockResolvedValue(specialWork),
  });

  render(<WorkDetailPage />);

  expect(await screen.findByText('SP01 - 露营小剧场')).toBeVisible();
  expect(screen.getByText('SP02 - 温泉小剧场')).toBeVisible();
  expect(screen.getAllByText('SP01').length).toBeGreaterThan(0);
  expect(screen.getAllByText('SP02').length).toBeGreaterThan(0);
});

test('详情首屏优先复用本地图片，不重复请求同一张背景图', async () => {
  const { container } = render(<WorkDetailPage />);

  await screen.findByAltText('测试动画 logo');
  const hero = container.querySelector('.detail-hero-art') as HTMLImageElement;
  const logo = await screen.findByAltText('测试动画 logo');
  const episodeImage = container.querySelector('.episode-thumb img');

  expect(hero.getAttribute('src')).toContain('D%3A%2Fmirror%2F%E6%B5%8B%E8%AF%95%E5%8A%A8%E7%94%BB%2Ffanart.jpg');
  expect(hero).toHaveAttribute('loading', 'eager');
  expect(container.querySelector('.detail-hero-bg')).toBeNull();
  expect(logo.getAttribute('src')).toContain('D%3A%2Fmirror%2F%E6%B5%8B%E8%AF%95%E5%8A%A8%E7%94%BB%2Fclearlogo.png');
  expect(episodeImage).toHaveAttribute('loading', 'eager');
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

test('恢复旧版 Bangumi 同步标签，并保持它绑定当前 V4 Work', async () => {
  render(<WorkDetailPage />);

  const tag = await screen.findByRole('button', { name: /Bangumi 未匹配/ });
  expect(tag).toHaveClass('detail-sync-status');
  expect(tag).toHaveAttribute('aria-haspopup', 'dialog');
});

test('更多菜单只显示已经接通 V4 的操作', async () => {
  render(<WorkDetailPage />);

  fireEvent.click(await screen.findByRole('button', { name: '更多操作' }));

  expect(screen.getAllByRole('menuitem', { name: /文件夹/ }).length).toBeGreaterThan(0);
  expect(screen.queryByRole('menuitem', { name: '手动刮削' })).toBeVisible();
  expect(screen.queryByRole('menuitem', { name: '删除该作品' })).toBeVisible();
});
