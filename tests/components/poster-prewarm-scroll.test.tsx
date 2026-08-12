import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import PosterCard from '../../src/components/library/PosterCard';
import { useLibraryStore } from '../../src/stores/library';
import { resetScrollGestureForTests } from '../../src/utils/scrollGesture';

const getWorkDetail = vi.fn();
let addListenerSpy: ReturnType<typeof vi.spyOn>;

function work(workId: string) {
  return {
    work_id: workId,
    title: `作品${workId}`,
    original_title: `Work ${workId}`,
    year: 2024,
    rating: 8.5,
    show_type: 'anime_series',
    source: 'local',
    poster_path: '/local/poster.jpg',
    fanart_path: '',
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  resetScrollGestureForTests();
  addListenerSpy = vi.spyOn(window, 'addEventListener');
  addListenerSpy.mockClear();
  getWorkDetail.mockReset();
  getWorkDetail.mockResolvedValue({ work_id: 'w1' });
  useLibraryStore.setState({ getWorkDetail });
});

afterEach(() => {
  vi.useRealTimers();
});

test('滚动过程中悬停卡片不会触发详情预取', () => {
  render(<PosterCard work={work('w1')} />);
  const card = screen.getByRole('button', { name: /作品w1/ });

  fireEvent.scroll(window);
  fireEvent.pointerEnter(card);
  vi.advanceTimersByTime(200);

  expect(getWorkDetail).not.toHaveBeenCalled();
});

test('滚动开始会取消已排队的详情预取', () => {
  render(<PosterCard work={work('w1')} />);
  const card = screen.getByRole('button', { name: /作品w1/ });

  fireEvent.pointerEnter(card);
  vi.advanceTimersByTime(50);
  fireEvent.scroll(window);
  vi.advanceTimersByTime(150);

  expect(getWorkDetail).not.toHaveBeenCalled();
});

test('滚动停止超过静默窗口后悬停恢复详情预取', () => {
  render(<PosterCard work={work('w1')} />);
  const card = screen.getByRole('button', { name: /作品w1/ });

  fireEvent.scroll(window);
  vi.advanceTimersByTime(350);
  fireEvent.pointerEnter(card);
  vi.advanceTimersByTime(200);

  expect(getWorkDetail).toHaveBeenCalledTimes(1);
});

test('多张海报卡共享同一个全局滚动监听器与计时器', async () => {
  const { unmount } = render(
    <div>
      {['w1', 'w2', 'w3', 'w4', 'w5', 'w6'].map((workId) => (
        <PosterCard key={workId} work={work(workId)} />
      ))}
    </div>,
  );

  await act(async () => {});
  const scrollListenerRegistrations = addListenerSpy.mock.calls.filter((call) => call[0] === 'scroll');
  expect(scrollListenerRegistrations).toHaveLength(1);

  fireEvent.scroll(window);
  expect(vi.getTimerCount()).toBe(1);

  unmount();
  await act(async () => {});
  expect(addListenerSpy.mock.calls.filter((call) => call[0] === 'scroll')).toHaveLength(1);
});

test('卸载海报卡不残留每卡滚动监听与计时器', async () => {
  const { unmount } = render(<PosterCard work={work('w1')} />);
  const card = screen.getByRole('button', { name: /作品w1/ });

  fireEvent.pointerEnter(card);
  unmount();
  await act(async () => {});
  fireEvent.scroll(window);

  expect(addListenerSpy.mock.calls.filter((call) => call[0] === 'scroll')).toHaveLength(1);
  expect(vi.getTimerCount()).toBe(1);
});

test('本地缩略图请求失败时回退同一张本地原图', async () => {
  render(<PosterCard work={work('w1')} thumbnailWidth={384} />);
  const image = screen.getByRole('img', { name: '作品w1' });

  expect(image.getAttribute('src')).toContain('/api/assets/thumbnail?path=');
  fireEvent.error(image);

  await act(async () => {});
  expect(image.getAttribute('src')).toContain('/api/assets?path=');
  expect(image.getAttribute('src')).not.toContain('/api/assets/thumbnail');
});

test('分类页在索引保留远程引用时仍优先使用本地镜像海报', () => {
  const remoteWork = {
    ...work('w1'),
    poster_path: 'https://image.tmdb.org/t/p/w342/remote.jpg',
    local_poster_path: 'D:/mirror/作品w1/poster.jpg',
  };

  render(<PosterCard work={remoteWork} thumbnailWidth={384} localArtworkOnly />);
  const image = screen.getByRole('img', { name: '作品w1' });

  expect(image.getAttribute('src')).toContain('/api/assets/thumbnail?path=');
  expect(image.getAttribute('src')).toContain('D%3A%2Fmirror%2F');
  expect(image.getAttribute('src')).not.toContain('/api/assets/remote');
});

test('本地海报缺失时分类卡片回退渲染远程海报', () => {
  const remoteOnlyWork = {
    ...work('w1'),
    poster_path: 'https://image.tmdb.org/t/p/w342/remote-only.jpg',
    local_poster_path: '',
  };

  render(<PosterCard work={remoteOnlyWork} thumbnailWidth={384} localArtworkOnly />);
  const image = screen.getByRole('img', { name: '作品w1' });

  expect(image.getAttribute('src')).toContain('/api/assets/remote?url=');
  expect(image.getAttribute('src')).toContain('remote-only');
  expect(image.getAttribute('src')).not.toContain('/api/assets/thumbnail');
});

test('本地与远程图都缺失时分类卡片显示文字占位', () => {
  const emptyWork = {
    ...work('w1'),
    poster_path: '',
    local_poster_path: '',
    fanart_path: '',
  };

  render(<PosterCard work={emptyWork} thumbnailWidth={384} localArtworkOnly />);

  expect(screen.queryByRole('img', { name: '作品w1' })).toBeNull();
  expect(screen.getAllByText('作品w1').length).toBeGreaterThan(0);
});

test('recent 卡片本地背景缺失时回退远程 fanart', () => {
  const recentWork = {
    ...work('w1'),
    fanart_path: 'https://image.tmdb.org/t/p/w1280/remote-fanart.jpg',
    local_fanart_path: '',
    poster_path: '',
  };

  render(<PosterCard work={recentWork} showType="recent" localArtworkOnly />);
  const image = screen.getByRole('img', { name: '作品w1' });

  expect(image.getAttribute('src')).toContain('/api/assets/remote?url=');
  expect(image.getAttribute('src')).toContain('remote-fanart');
  expect(image.getAttribute('src')).not.toContain('/api/assets/thumbnail');
});
