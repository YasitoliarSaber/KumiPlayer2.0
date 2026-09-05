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

test('季度缺少剧照时仍使用统一的横向剧集组件', async () => {
  const withoutThumbs = {
    ...work,
    seasons: [{ season_id: 'season-2', season_number: 2, group_type: 'season', label: '第 2 季', episode_count: 1 }],
    episodes: [{ ...work.episodes[0], episode_id: 'episode-2', season_number: 2, thumb_path: '' }],
  };
  useUiStore.setState({ selectedSeasonNumber: 2 });
  useLibraryStore.setState({
    works: [withoutThumbs as never],
    getWorkDetail: vi.fn().mockResolvedValue(withoutThumbs),
  });

  const { container } = render(<WorkDetailPage />);

  expect(await screen.findByText('启程')).toBeVisible();
  expect(container.querySelector('.detail-episode-grid.thumbnail-strip')).not.toBeNull();
  expect(screen.getByRole('button', { name: '上一组剧集' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '列表视图' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '网格视图' })).not.toBeInTheDocument();
});

test('首次进入优先显示正片季度并把特别篇排在最后', async () => {
  const mixedSeasons = {
    ...work,
    seasons: [
      { season_id: 'season-special', season_number: 0, group_type: 'special', label: '特别篇', episode_count: 1 },
      { season_id: 'season-2', season_number: 2, group_type: 'season', label: '第 2 季', episode_count: 1 },
      { season_id: 'season-1', season_number: 1, group_type: 'season', label: '第 1 季', episode_count: 1 },
    ],
    episodes: [
      { ...work.episodes[0], episode_id: 'special-1', season_number: 0, episode_number: null, special_number: 1, title: '露营小剧场', group_type: 'special', kind: 'special' },
      { ...work.episodes[0], episode_id: 'episode-s2', season_number: 2, title: '第二季启程' },
      { ...work.episodes[0], episode_id: 'episode-s1', season_number: 1, title: '第一季启程' },
    ],
  };
  useUiStore.setState({ selectedSeasonNumber: null, selectedSeasonByWork: {} });
  useLibraryStore.setState({
    works: [mixedSeasons as never],
    getWorkDetail: vi.fn().mockResolvedValue(mixedSeasons),
  });

  render(<WorkDetailPage />);

  expect(await screen.findByText('第一季启程')).toBeVisible();
  expect(screen.queryByText('露营小剧场')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('combobox', { name: '选择季度' }));
  const options = screen.getAllByRole('option').map((option) => option.textContent);
  expect(options).toEqual(['第1季', '第2季', '特别篇']);
});

test('用户切换季度后重新进入同一作品仍恢复该季度', async () => {
  const multiSeasonWork = {
    ...work,
    seasons: [
      { season_id: 'season-1', season_number: 1, group_type: 'season', label: '第 1 季', episode_count: 1 },
      { season_id: 'season-2', season_number: 2, group_type: 'season', label: '第 2 季', episode_count: 1 },
    ],
    episodes: [
      { ...work.episodes[0], episode_id: 'episode-s1', season_number: 1, title: '第一季启程' },
      { ...work.episodes[0], episode_id: 'episode-s2', season_number: 2, title: '第二季启程' },
    ],
  };
  useUiStore.setState({ selectedSeasonNumber: null, selectedSeasonByWork: {} });
  useLibraryStore.setState({
    works: [multiSeasonWork as never],
    getWorkDetail: vi.fn().mockResolvedValue(multiSeasonWork),
  });

  const firstVisit = render(<WorkDetailPage />);
  expect(await screen.findByText('第一季启程')).toBeVisible();
  fireEvent.click(screen.getByRole('combobox', { name: '选择季度' }));
  fireEvent.click(screen.getByRole('option', { name: '第 2 季' }));
  expect(await screen.findByText('第二季启程')).toBeVisible();
  expect(useUiStore.getState().selectedSeasonByWork[work.work_id]).toEqual({
    seasonNumber: 2,
    seasonKey: 'season:2',
  });

  firstVisit.unmount();
  render(<WorkDetailPage />);

  expect(await screen.findByText('第二季启程')).toBeVisible();
  expect(screen.queryByText('第一季启程')).not.toBeInTheDocument();
});

test('缺图剧集卡显示占位，不把背景大图复制为每集缩略图', async () => {
  const withoutThumbs = {
    ...work,
    episodes: [{ ...work.episodes[0], episode_id: 'episode-no-thumb', thumb_path: '' }],
  };
  useLibraryStore.setState({
    works: [withoutThumbs as never],
    getWorkDetail: vi.fn().mockResolvedValue(withoutThumbs),
  });

  const { container } = render(<WorkDetailPage />);

  expect(await screen.findByText('启程')).toBeVisible();
  const episodeThumb = container.querySelector('.episode-thumb');
  expect(episodeThumb).not.toBeNull();
  // 缺图时不得回退到整页背景图（fanart），否则几十张卡片会并发请求同一张原图。
  expect(episodeThumb?.querySelector('img')).toBeNull();
  expect(episodeThumb?.querySelector('.episode-thumb-placeholder')).not.toBeNull();
  expect(screen.getByRole('button', { name: /播放第 1 集：启程/ })).toBeVisible();
});

test('特别篇不展示内部 SP 编号，并显示彼此可区分的本地标题', async () => {
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

  // SP 编号仅用于后端排序/定位，用户界面只显示可区分的语义标题。
  expect(await screen.findByText('露营小剧场')).toBeVisible();
  expect(screen.getByText('温泉小剧场')).toBeVisible();
  expect(screen.queryByText('SP01')).not.toBeInTheDocument();
  expect(screen.queryByText('SP02')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '播放特别篇 1：露营小剧场' })).toBeVisible();
});

test('历史数据标题残留的 SP 前缀不会暴露在特别篇界面', async () => {
  const legacySpecialWork = {
    ...work,
    seasons: [
      { season_id: 'season-legacy-special', season_number: 0, group_type: 'special', label: '特别篇', episode_count: 1 },
    ],
    episodes: [
      {
        ...work.episodes[0],
        episode_id: 'special-legacy',
        season_number: 0,
        episode_number: null,
        special_number: 8,
        title: 'SP08 - Making Documentary',
        group_type: 'special',
        kind: 'special',
      },
    ],
  };
  useUiStore.setState({ selectedSeasonNumber: 0 });
  useLibraryStore.setState({
    works: [legacySpecialWork as never],
    getWorkDetail: vi.fn().mockResolvedValue(legacySpecialWork),
  });

  render(<WorkDetailPage />);

  expect(await screen.findByText('Making Documentary')).toBeVisible();
  expect(screen.queryByText('SP08')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '播放特别篇 8：Making Documentary' })).toBeVisible();
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
