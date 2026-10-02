import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
      { season_id: 'season-unknown', season_number: null, group_type: 'unassigned', label: '未分季', episode_count: 1 },
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

  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择季度' })).toHaveTextContent('第1季'));
  expect(screen.getByText('第一季启程')).toBeVisible();
  expect(screen.queryByText('露营小剧场')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('combobox', { name: '选择季度' }));
  const options = screen.getAllByRole('option').map((option) => option.textContent);
  expect(options).toEqual(['第1季', '第2季', '未分季', '特别篇']);
  fireEvent.click(screen.getByRole('option', { name: '特别篇' }));
  expect(screen.getByRole('button', { name: '播放特别篇 1：露营小剧场' })).toBeVisible();
  expect(screen.queryByText('第一季启程')).not.toBeInTheDocument();
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

const activePlayback = {
  status: 'playing',
  session: {
    session_id: 'detail-session', status: 'playing', work_id: 'work-v4',
    episode_id: 'episode-1', asset_id: 'asset-primary', playback_locator: '',
  },
};
const savedProgress = {
  work_id: 'work-v4', episode_id: 'episode-1', asset_id: 'asset-primary',
  position: 120, duration: 600, completed: false, updated_at: '',
};

test('详情主按钮按真实播放器会话显示正在播放并阻止重复启动', async () => {
  vi.mocked(playbackApi.getStatus).mockResolvedValue(activePlayback);
  const play = vi.spyOn(playbackApi, 'play');
  render(<WorkDetailPage />);
  const button = await screen.findByRole('button', { name: /正在播放.*S01E01/ });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(play).not.toHaveBeenCalled();
});

test('有未看完进度显示继续播放，零进度和已完成仍显示开始播放', async () => {
  vi.mocked(playbackApi.getProgress).mockResolvedValue({ items: [savedProgress] });
  const { unmount } = render(<WorkDetailPage />);
  expect(await screen.findByRole('button', { name: /继续播放.*S01E01/ })).toBeEnabled();
  unmount();
  vi.mocked(playbackApi.getProgress).mockResolvedValue({ items: [{ ...savedProgress, completed: true }] });
  render(<WorkDetailPage />);
  expect(await screen.findByRole('button', { name: /开始播放.*S01E01/ })).toBeEnabled();
});

test('播放成功立即显示正在播放，不等待历史刷新', async () => {
  vi.spyOn(useLibraryStore.getState(), 'refreshHistory').mockImplementation(() => new Promise(() => {}));
  vi.spyOn(playbackApi, 'play').mockResolvedValue(activePlayback.session);
  render(<WorkDetailPage />);
  fireEvent.click(await screen.findByRole('button', { name: /开始播放.*S01E01/ }));
  expect(await screen.findByRole('button', { name: /正在播放.*S01E01/ })).toBeDisabled();
});

test('播放器退出后回到窗口立即按保存进度显示继续播放', async () => {
  vi.mocked(playbackApi.getStatus).mockResolvedValue(activePlayback);
  render(<WorkDetailPage />);
  await screen.findByRole('button', { name: /正在播放.*S01E01/ });
  vi.mocked(playbackApi.getStatus).mockResolvedValue({
    status: 'exited', session: { ...activePlayback.session, status: 'exited' },
  });
  vi.mocked(playbackApi.getProgress).mockResolvedValue({ items: [savedProgress] });
  fireEvent(window, new Event('focus'));
  expect(await screen.findByRole('button', { name: /继续播放.*S01E01/ })).toBeEnabled();
});

test('播放请求失败不会显示正在播放，也不会锁住按钮', async () => {
  vi.spyOn(window, 'alert').mockImplementation(() => {});
  vi.spyOn(playbackApi, 'play').mockRejectedValue(new Error('无法启动播放器'));
  render(<WorkDetailPage />);
  const button = await screen.findByRole('button', { name: /开始播放.*S01E01/ });
  fireEvent.click(button);
  await waitFor(() => expect(window.alert).toHaveBeenCalledWith('无法启动播放器'));
  expect(button).toBeEnabled();
  expect(screen.queryByRole('button', { name: /正在播放.*S01E01/ })).not.toBeInTheDocument();
});

test('启动前的迟到状态查询不能覆盖已确认的正在播放状态', async () => {
  let resolveOldStatus!: (status: Awaited<ReturnType<typeof playbackApi.getStatus>>) => void;
  vi.mocked(playbackApi.getStatus).mockReturnValueOnce(new Promise((resolve) => { resolveOldStatus = resolve; }));
  vi.spyOn(useLibraryStore.getState(), 'refreshHistory').mockImplementation(() => new Promise(() => {}));
  vi.spyOn(playbackApi, 'play').mockResolvedValue(activePlayback.session);
  render(<WorkDetailPage />);
  fireEvent.click(await screen.findByRole('button', { name: /开始播放.*S01E01/ }));
  await screen.findByRole('button', { name: /正在播放.*S01E01/ });
  await act(async () => resolveOldStatus({ status: 'idle', session: null }));
  expect(screen.getByRole('button', { name: /正在播放.*S01E01/ })).toBeDisabled();
});

test('播放器切到下一集后主按钮同步真实剧集身份', async () => {
  const twoEpisodes = {
    ...work,
    episodes: [work.episodes[0], { ...work.episodes[0], episode_id: 'episode-2', episode_number: 2, title: '继续旅程' }],
  };
  useLibraryStore.setState({ works: [twoEpisodes as never], getWorkDetail: vi.fn().mockResolvedValue(twoEpisodes) });
  vi.mocked(playbackApi.getStatus).mockResolvedValue(activePlayback);
  render(<WorkDetailPage />);
  await screen.findByRole('button', { name: /正在播放.*S01E01/ });
  vi.mocked(playbackApi.getStatus).mockResolvedValue({
    status: 'playing', session: { ...activePlayback.session, episode_id: 'episode-2' },
  });
  fireEvent(window, new Event('focus'));
  expect(await screen.findByRole('button', { name: /正在播放.*S01E02.*继续旅程/ })).toBeDisabled();
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
  expect(screen.queryByRole('menuitem', { name: /重新刮削/ })).toBeVisible();
  expect(screen.queryByRole('menuitem', { name: '删除该作品' })).toBeVisible();
});

test('缺图特别篇整组使用紧凑列表，混合图片仍保持集顺序和播放身份', async () => {
  const play = vi.spyOn(playbackApi, 'play').mockResolvedValue({ ok: true, session_id: 'special-session' } as never);
  const specialWork = {
    ...work,
    seasons: [{ season_id: 'specials', season_number: 0, group_type: 'special', label: '特别篇', episode_count: 14 }],
    episodes: Array.from({ length: 14 }, (_, index) => ({
      ...work.episodes[0], episode_id: `special-${index + 1}`, season_number: 0,
      episode_number: null, special_number: index + 1, group_type: 'special', kind: 'special',
      title: `SP${index + 1} - 小剧场 ${index + 1}`, thumb_path: index === 1 ? work.episodes[0].thumb_path : '',
    })),
  };
  useUiStore.setState({ selectedSeasonNumber: 0, selectedSeasonByWork: {} });
  useLibraryStore.setState({ works: [specialWork as never], getWorkDetail: vi.fn().mockResolvedValue(specialWork) });
  const { container } = render(<WorkDetailPage />);
  await waitFor(() => expect(container.querySelector('[aria-busy="false"]')).not.toBeNull());
  await screen.findByText('小剧场 1');
  expect(screen.getByText('小剧场 10')).toBeVisible();
  const list = container.querySelector('.detail-special-list')!;
  expect(list).not.toBeNull();
  expect(Array.from(list.children, (row) => (row as HTMLElement).dataset.episodeId)).toEqual(
    specialWork.episodes.map((episode) => episode.episode_id),
  );
  expect(container.querySelector('.detail-episode-grid.thumbnail-strip')).toBeNull();
  expect(list.querySelectorAll('.episode-thumb')).toHaveLength(0);
  expect(list.querySelectorAll('img')).toHaveLength(1);
  expect(screen.queryByRole('button', { name: '上一组剧集' })).not.toBeInTheDocument();
  const reveal = vi.fn();
  Object.defineProperty(list.children[9], 'scrollIntoView', { configurable: true, value: reveal });
  fireEvent.click(screen.getByRole('button', { name: '快速选集' }));
  const chooser = screen.getByRole('dialog', { name: '快速选集' });
  fireEvent.click(within(chooser).getByRole('button', { name: '小剧场 10' }));
  expect(reveal).toHaveBeenCalledWith({ block: 'center', inline: 'nearest', behavior: 'smooth' });
  expect(screen.queryByRole('dialog', { name: '快速选集' })).not.toBeInTheDocument();
  expect(list.children[9].querySelector('button')).toHaveFocus();
  fireEvent.click(within(list as HTMLElement).getByRole('button', { name: '播放特别篇 2：小剧场 2' }));
  await waitFor(() => expect(play).toHaveBeenCalledWith({ work_id: work.work_id, episode_id: 'special-2', asset_id: 'asset-primary' }));
  fireEvent.contextMenu(list.children[0]);
  expect(await screen.findByRole('menuitem', { name: '已看完' })).toBeVisible();
});

test('全部有图的特别篇也显示原名列表', async () => {
  const specialWork = {
    ...work,
    seasons: [{ season_id: 'specials', season_number: 0, group_type: 'special', label: '特别篇', episode_count: 1 }],
    episodes: [{ ...work.episodes[0], episode_id: 'special-pictured', season_number: 0, special_number: 1, title: 'SP01 - 图片小剧场', original_filename: '[Group] Show - 14.5 [1080p].mkv', group_type: 'special', kind: 'special' }],
  };
  useUiStore.setState({ selectedSeasonNumber: 0, selectedSeasonByWork: {} });
  useLibraryStore.setState({ works: [specialWork as never], getWorkDetail: vi.fn().mockResolvedValue(specialWork) });
  const { container } = render(<WorkDetailPage />);
  await screen.findByText('[Group] Show - 14.5 [1080p].mkv');
  expect(container.querySelector('.detail-episode-grid.thumbnail-strip')).toBeNull();
  expect(container.querySelector('.detail-special-list')).not.toBeNull();
});

test('关联与推荐优先本地海报，只有横图或图片失败时保留文字占位', async () => {
  const related = { ...work, work_id: 'related', title: '关联作品甲', local_poster_path: 'D:/mirror/related-poster.jpg', fanart_path: 'https://image.tmdb.org/t/p/original/related-fanart.jpg' };
  const similar = { ...work, work_id: 'similar', title: '推荐作品乙', local_poster_path: 'D:/mirror/similar-poster.jpg' };
  const noPoster = { ...work, work_id: 'no-poster', title: '只有背景图', local_poster_path: '', poster_path: '', fanart_path: 'https://image.tmdb.org/t/p/original/only-fanart.jpg' };
  const detailWork = { ...work, related_works: [{ work_id: related.work_id, title: related.title, poster_path: 'https://image.tmdb.org/t/p/w500/remote.jpg', fanart_path: related.fanart_path }, { work_id: noPoster.work_id, title: noPoster.title, fanart_path: noPoster.fanart_path }] };
  useLibraryStore.setState({ works: [detailWork, related, similar, noPoster] as never, getWorkDetail: vi.fn().mockResolvedValue(detailWork) });
  const { container } = render(<WorkDetailPage />);
  await screen.findByText('关联作品甲');
  const relatedCard = container.querySelector('.detail-related-card')!;
  expect(relatedCard.querySelector('img')?.getAttribute('src')).toContain(encodeURIComponent(related.local_poster_path));
  expect(container.querySelector('.detail-similar-card img')?.getAttribute('src')).toContain(encodeURIComponent(similar.local_poster_path));
  expect(container.querySelectorAll('.detail-related-card')[1].querySelector('img')).toBeNull();
  expect(container.querySelectorAll('.detail-related-card')[1].querySelector('.detail-related-fallback')).toHaveTextContent('暂无海报');
  fireEvent.error(relatedCard.querySelector('img')!);
  expect(relatedCard.querySelector('.detail-related-fallback')).toHaveTextContent('暂无海报');
  expect(relatedCard.querySelector('.detail-related-copy')).toHaveTextContent('关联作品甲');
});
