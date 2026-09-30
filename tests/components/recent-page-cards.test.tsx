import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import RecentPage from '../../src/pages/RecentPage';
import { useLibraryStore } from '../../src/stores/library';
import { useUiStore } from '../../src/stores/ui';
import type { PlaybackHistoryItem, WorkIndex } from '../../src/api/types';

const openWorkDetail = vi.fn();
const work = {
  work_id: 'w-recent', title: '异世界归来的舅舅', original_title: '',
  show_type: 'anime_series', media_type: 'tv', source: 'local',
  fanart_path: '/work-backdrop.jpg', poster_path: '/work-poster.jpg',
} as WorkIndex;
const history = {
  work_id: work.work_id, episode_id: 'e-six', asset_id: 'a-six',
  season_number: 1, episode_number: 6, position: 694, duration: 1422,
  completed: false, updated_at: '2026-09-30T12:40:00',
  thumb_path: '/mirror/episode-six-thumb.jpg',
} as PlaybackHistoryItem;

beforeEach(() => {
  openWorkDetail.mockReset();
  useUiStore.setState({ source: 'all' });
  useLibraryStore.setState({ works: [work], history: [history], loading: false, error: null, openWorkDetail });
});
afterEach(cleanup);

test('最近观看使用对应集缩略图，并将编号、进度、时间分行显示', () => {
  render(<RecentPage />);
  const card = screen.getByRole('button', { name: /异世界归来的舅舅.*S01E06/ });
  const image = screen.getByRole('img');
  expect(image.getAttribute('src')).toContain('episode-six-thumb');
  expect(image.getAttribute('src')).not.toContain('work-backdrop');
  expect(screen.getByText('S01E06')).toBeVisible();
  expect(screen.getByText('11:34 / 23:42 · 49%')).toBeVisible();
  expect(screen.queryByText(/最近播放：/)).toBeNull();
  expect(card.querySelector('time')).toHaveAttribute('datetime', history.updated_at);
  fireEvent.click(card);
  expect(openWorkDetail).toHaveBeenCalledWith(work.work_id);
});

test('剧集缩略图缺失时显示占位，不用作品背景冒充分集画面', () => {
  useLibraryStore.setState({ history: [{ ...history, thumb_path: '' } as PlaybackHistoryItem] });
  render(<RecentPage />);
  expect(screen.queryByRole('img')).toBeNull();
  expect(screen.getByText('暂无分集图片')).toBeVisible();
  expect(screen.getByText('S01E06')).toBeVisible();
});

test('最近观看的数量与实际卡片一致，窄屏也不丢掉记录', () => {
  const works = Array.from({ length: 7 }, (_, i) => ({ ...work, work_id: `w-${i}`, title: `作品 ${i}` }));
  useLibraryStore.setState({ works, history: works.map((item) => ({ ...history, work_id: item.work_id })) });
  render(<RecentPage />);
  expect(screen.getByText('最近 7 部')).toBeVisible();
  expect(screen.getAllByRole('button', { name: /作品/ })).toHaveLength(7);
});
