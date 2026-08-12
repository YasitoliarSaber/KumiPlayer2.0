import { StrictMode } from 'react';
import { render } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { useUiStore } from '../../src/stores/ui';
import { useLibraryStore } from '../../src/stores/library';

vi.mock('../../src/api/tracking', () => ({
  trackingApi: {
    list: vi.fn().mockResolvedValue({ items: [] }),
    scanAll: vi.fn(),
    importRoot: vi.fn(),
    scan: vi.fn(),
    create: vi.fn(),
  },
}));

vi.mock('../../src/api/tasks', () => ({
  tasksApi: {
    get: vi.fn(),
    list: vi.fn().mockResolvedValue({ tasks: [] }),
    cancel: vi.fn(),
  },
}));

vi.mock('../../src/platform/folderPicker', () => ({
  pickFolder: vi.fn(),
}));

vi.mock('../../src/components/library/VirtualizedPosterGrid', () => ({
  default: () => <div data-testid="poster-grid" />,
}));

vi.mock('../../src/components/library/LibraryViewControls', () => ({
  default: () => <div data-testid="view-controls" />,
  normalizeColumns: (size: number) => Math.max(4, Math.round(size / 180)),
}));

import CategoryPage from '../../src/pages/CategoryPage';

function makeWork() {
  return {
    work_id: 'w1',
    title: '测试作品',
    original_title: '',
    year: 2024,
    rating: 0,
    show_type: 'anime_series',
    source: 'all',
    poster_path: '',
    fanart_path: '',
  };
}

let scrollToSpy: ReturnType<typeof vi.fn>;
let mainElement: { scrollTop: number; scrollTo: ReturnType<typeof vi.fn> };
let querySelectorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  scrollToSpy = vi.fn();
  mainElement = { scrollTop: 0, scrollTo: scrollToSpy };
  querySelectorSpy = vi.spyOn(document, 'querySelector').mockImplementation((selector: string) => (
    selector === '.app-main' ? mainElement : null
  ));
  useUiStore.setState({
    page: 'category',
    activeCategory: 'anime_series',
    selectedWorkId: null,
    source: 'all',
    categoryScrollRestore: { category: 'anime_series', source: 'all', scrollTop: 640 },
  });
  useLibraryStore.setState({
    works: [makeWork()],
    history: [],
    loading: false,
    error: null,
    loadLibrary: vi.fn(),
  });
});

afterEach(() => {
  querySelectorSpy.mockRestore();
  vi.useRealTimers();
});

test('分类页挂载后在 rAF 内消费待恢复位置并滚动到原位置', () => {
  render(<CategoryPage />);

  expect(scrollToSpy).not.toHaveBeenCalled();
  vi.advanceTimersByTime(20);

  expect(scrollToSpy).toHaveBeenCalledWith({ top: 640, behavior: 'auto' });
  expect(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all')).toBeNull();
});

test('无待恢复位置时分类页挂载后回到顶部', () => {
  useUiStore.setState({ categoryScrollRestore: null });

  render(<CategoryPage />);
  vi.advanceTimersByTime(20);

  expect(scrollToSpy).toHaveBeenCalledWith({ top: 0, behavior: 'auto' });
});

test('Strict Mode 开发态双跑 effect 后 restore 只消费一次且不置顶', () => {
  render(
    <StrictMode>
      <CategoryPage />
    </StrictMode>,
  );

  vi.advanceTimersByTime(20);

  expect(scrollToSpy).toHaveBeenCalledWith({ top: 640, behavior: 'auto' });
  expect(scrollToSpy).not.toHaveBeenCalledWith({ top: 0, behavior: 'auto' });
  expect(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all')).toBeNull();
});

test('完整路径：分类页滚动到中间 → goDetail 保存 → 返回 → 重新挂载恢复', () => {
  const { unmount } = render(<CategoryPage />);
  vi.advanceTimersByTime(20);
  scrollToSpy.mockClear();

  // 用户滚动到中间，.app-main 的真实 scrollTop 变为 2000
  mainElement.scrollTop = 2000;

  // 点击作品 → openWorkDetail → commitDetailNavigation → goDetail
  useUiStore.getState().goDetail('w1');
  expect(useUiStore.getState().categoryScrollRestore).toEqual({
    category: 'anime_series',
    source: 'all',
    scrollTop: 2000,
  });

  // 详情页挂载，分类页卸载
  unmount();

  // 标题栏返回 → goBack 恢复历史位置
  useUiStore.getState().goBack();
  expect(useUiStore.getState().page).toBe('category');

  // 分类页重新挂载，rAF 内恢复滚动位置
  render(<CategoryPage />);
  vi.advanceTimersByTime(20);

  expect(scrollToSpy).toHaveBeenCalledWith({ top: 2000, behavior: 'auto' });
  expect(scrollToSpy).not.toHaveBeenCalledWith({ top: 0, behavior: 'auto' });
  expect(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all')).toBeNull();
});
