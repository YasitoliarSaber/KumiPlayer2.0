/** P-006 新番追更入口：分类页扫描按钮、追更卡标签。 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import CategoryPage from '../../src/pages/CategoryPage';
import PosterCard from '../../src/components/library/PosterCard';

const api = vi.hoisted(() => ({
  trackingScanAll: vi.fn(),
  trackingCancelScan: vi.fn(),
  trackingWorks: vi.fn(),
  trackingScanWork: vi.fn(),
  sourceLibraries: vi.fn(),
  openlistStatus: vi.fn(),
  drafts: vi.fn(),
}));
vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: api }));
vi.mock('../../src/api/library', () => ({ v4LibraryApi: { getWorkDetail: vi.fn().mockResolvedValue({}) } }));
vi.mock('../../src/stores/library', () => ({
  useLibraryStore: (selector: (state: unknown) => unknown) => selector({
    works: [
      { work_id: 'w1', title: '追更作品', year: 2026, media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: { work_id: 'w1', status: 'watching', note: '', favorite: false, updated_at: 'now' }, latest_episode_number: 5 },
      { work_id: 'w2', title: '普通作品', year: 2026, media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: null },
    ],
    history: [],
    loading: false,
    error: null,
  }),
}));
vi.mock('../../src/stores/ui', () => {
  const state = {
    activeCategory: 'seasonal',
    source: 'all',
    sort: 'recent',
    setSort: vi.fn(),
    posterSize: 'medium',
    consumeCategoryScrollRestore: vi.fn().mockReturnValue(0),
  };
  const useUiStore = (selector?: (value: typeof state) => unknown) => selector ? selector(state) : state;
  useUiStore.getState = () => state;
  return { useUiStore };
});
vi.mock('../../src/platform/folderPicker', () => ({ pickDirectoryTreeFile: vi.fn(), pickFolder: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  api.trackingScanAll.mockResolvedValue({
    tasks: [
      { task_id: 'scan-1', root_id: 'r1', remote_root: '/Anime', status: 'running' },
    ],
  });
  api.trackingCancelScan.mockResolvedValue({ scan_id: 'scan-1', status: 'cancelling' });
});

test('新番分类显示扫描入口并调用 V4 tracking 命令', async () => {
  render(<CategoryPage />);
  expect(screen.getByText('扫描新番（1 部）')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: /扫描新番/ }));
  await waitFor(() => expect(api.trackingScanAll).toHaveBeenCalled());
  expect(await screen.findByText(/已开始 1 个新番增量扫描/)).toBeVisible();
});

test('新番扫描可取消', async () => {
  render(<CategoryPage />);
  fireEvent.click(screen.getByRole('button', { name: /扫描新番/ }));
  const cancel = await screen.findByRole('button', { name: '取消扫描' });
  fireEvent.click(cancel);
  await waitFor(() => expect(api.trackingCancelScan).toHaveBeenCalledWith('scan-1'));
});

test('PosterCard 显示追更标签与最新集摘要，未追更不显示', () => {
  const { rerender } = render(<PosterCard work={{ work_id: 'w1', title: '追更作品', media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: { work_id: 'w1', status: 'watching', note: '', favorite: false, updated_at: 'now' }, latest_episode_number: 5 } as never} index={0} />);
  expect(screen.getByText(/追更中 · 更新至 5 集/)).toBeVisible();

  rerender(<PosterCard work={{ work_id: 'w2', title: '普通作品', media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: null } as never} index={0} />);
  expect(screen.queryByText(/追更中/)).toBeNull();
});
