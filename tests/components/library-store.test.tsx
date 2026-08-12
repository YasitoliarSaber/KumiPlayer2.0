import { beforeEach, expect, test, vi } from 'vitest';
import type { WorkIndex } from '../../src/api/types';

const { getLibrary, getWorkDetail, getHistory } = vi.hoisted(() => ({
  getLibrary: vi.fn(),
  getWorkDetail: vi.fn(),
  getHistory: vi.fn(),
}));

vi.mock('../../src/api/library', () => ({
  libraryApi: { getLibrary, getWorkDetail },
}));

vi.mock('../../src/api/playback', () => ({
  playbackApi: { getHistory },
}));

import { useLibraryStore } from '../../src/stores/library';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function work(workId: string): WorkIndex {
  return {
    work_id: workId,
    title: workId,
    original_title: '',
    year: null,
    rating: 0,
    plot: '',
    genres: [],
    studios: [],
    show_type: '',
    media_type: '',
    source: 'pan115',
    card_type: 'standalone',
    poster_path: '',
    fanart_path: '',
    clearlogo_path: '',
    dir_path: '',
    seasons: [],
    episodes: [],
    related_works: [],
    tags: [],
    last_played: null,
  };
}

beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  getLibrary.mockReset();
  getWorkDetail.mockReset();
  getHistory.mockReset();
  localStorage.clear();
  useLibraryStore.setState({
    works: [],
    history: [],
    loading: false,
    error: null,
    loaded: false,
    selectedWorkDetail: null,
    detailLoading: false,
    detailError: null,
  });
});

test('删除后的强制刷新不会被进行中的加载拦截，旧响应也不能恢复已删除卡片', async () => {
  const staleLibrary = deferred<{ works: WorkIndex[] }>();
  const staleHistory = deferred<{ items: [] }>();
  getLibrary
    .mockImplementationOnce(() => staleLibrary.promise)
    .mockResolvedValueOnce({ works: [] });
  getHistory
    .mockImplementationOnce(() => staleHistory.promise)
    .mockResolvedValueOnce({ items: [] });

  const staleRequest = useLibraryStore.getState().loadLibrary();
  expect(useLibraryStore.getState().loading).toBe(true);

  await useLibraryStore.getState().loadLibrary({ force: true });
  expect(getLibrary).toHaveBeenCalledTimes(2);
  expect(useLibraryStore.getState().works).toEqual([]);

  staleLibrary.resolve({ works: [work('deleted-work')] });
  staleHistory.resolve({ items: [] });
  await staleRequest;

  expect(useLibraryStore.getState().works).toEqual([]);
});

test('删除后的空库刷新会使先前排队的缓存写入失效', async () => {
  const idleCallbacks: IdleRequestCallback[] = [];
  const requestIdleCallback = vi.fn((callback: IdleRequestCallback) => {
    idleCallbacks.push(callback);
    return idleCallbacks.length;
  });
  Object.defineProperty(window, 'requestIdleCallback', {
    configurable: true,
    value: requestIdleCallback,
  });
  getLibrary
    .mockResolvedValueOnce({ works: [work('deleted-work')] })
    .mockResolvedValueOnce({ works: [] });
  getHistory.mockResolvedValue({ items: [] });

  await useLibraryStore.getState().loadLibrary();
  expect(idleCallbacks).toHaveLength(1);

  await useLibraryStore.getState().loadLibrary({ force: true });
  idleCallbacks[0]({ didTimeout: false, timeRemaining: () => 50 });

  expect(localStorage.getItem('kumiplayer-library-cache-v4')).toBeNull();
});

test('详情返回值缺少本地图片字段时保留 compact 卡片的本地路径', async () => {
  const compactWork = work('w1');
  compactWork.local_poster_path = 'D:/mirror/作品1/poster.jpg';
  compactWork.local_fanart_path = 'D:/mirror/作品1/fanart.jpg';
  (compactWork as any).local_clearlogo_path = 'D:/mirror/作品1/clearlogo.png';
  getLibrary.mockResolvedValue({ works: [compactWork] });
  getHistory.mockResolvedValue({ items: [] });
  getWorkDetail.mockResolvedValue({ ...work('w1') });

  await useLibraryStore.getState().loadLibrary();
  await useLibraryStore.getState().openWorkDetail('w1');

  const updated = useLibraryStore.getState().works[0];
  expect(updated.local_poster_path).toBe('D:/mirror/作品1/poster.jpg');
  expect(updated.local_fanart_path).toBe('D:/mirror/作品1/fanart.jpg');
  expect((updated as any).local_clearlogo_path).toBe('D:/mirror/作品1/clearlogo.png');
});

test('详情返回值带新本地路径时更新 compact 卡片', async () => {
  const compactWork = work('w2');
  compactWork.local_poster_path = 'D:/mirror/作品2/poster-old.jpg';
  getLibrary.mockResolvedValue({ works: [compactWork] });
  getHistory.mockResolvedValue({ items: [] });
  getWorkDetail.mockResolvedValue({
    ...work('w2'),
    local_poster_path: 'D:/mirror/作品2/poster-new.jpg',
  });

  await useLibraryStore.getState().loadLibrary();
  await useLibraryStore.getState().openWorkDetail('w2');

  const updated = useLibraryStore.getState().works[0];
  expect(updated.local_poster_path).toBe('D:/mirror/作品2/poster-new.jpg');
});

test('详情返回值本地路径为空时保留 compact 卡片的有效本地路径', async () => {
  const compactWork = work('w3');
  compactWork.local_fanart_path = 'D:/mirror/作品3/fanart.jpg';
  getLibrary.mockResolvedValue({ works: [compactWork] });
  getHistory.mockResolvedValue({ items: [] });
  getWorkDetail.mockResolvedValue({
    ...work('w3'),
    local_poster_path: '',
    local_fanart_path: '',
  });

  await useLibraryStore.getState().loadLibrary();
  await useLibraryStore.getState().openWorkDetail('w3');

  const updated = useLibraryStore.getState().works[0];
  expect(updated.local_fanart_path).toBe('D:/mirror/作品3/fanart.jpg');
});

test('来源筛选支持 OpenList：主来源与跨来源合并作品均可见', async () => {
  const { useUiStore } = await import('../../src/stores/ui');
  const openlistOnly = { ...work('openlist-1'), source: 'openlist' as const };
  const merged = {
    ...work('merged-1'),
    source: 'local' as const,
    sources: ['local', 'openlist'] as Array<'local' | 'openlist'>,
  };
  const pan115Only = work('pan115-1');
  getLibrary.mockResolvedValue({ works: [openlistOnly, merged, pan115Only] });
  getHistory.mockResolvedValue({ items: [] });

  await useLibraryStore.getState().loadLibrary();
  useUiStore.setState({ source: 'openlist' });

  const filtered = useLibraryStore.getState().filteredWorks();
  const ids = filtered.map((item) => item.work_id).sort();
  expect(ids).toEqual(['merged-1', 'openlist-1']);

  // 纯 115 作品不在 OpenList 筛选下出现
  expect(filtered.some((item) => item.work_id === 'pan115-1')).toBe(false);

  useUiStore.setState({ source: 'all' });
});

test('来源筛选不因 OpenList 新增改变本地来源语义', async () => {
  const { useUiStore } = await import('../../src/stores/ui');
  const localWork = { ...work('local-1'), source: 'local' as const };
  const openlistWork = { ...work('openlist-2'), source: 'openlist' as const };
  getLibrary.mockResolvedValue({ works: [localWork, openlistWork] });
  getHistory.mockResolvedValue({ items: [] });

  await useLibraryStore.getState().loadLibrary();
  useUiStore.setState({ source: 'local' });

  const filtered = useLibraryStore.getState().filteredWorks();
  expect(filtered.map((item) => item.work_id)).toEqual(['local-1']);

  useUiStore.setState({ source: 'all' });
});
