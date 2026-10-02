import { act, fireEvent, render, screen } from '@testing-library/react';
import { Profiler } from 'react';
import { beforeEach, expect, test, vi } from 'vitest';
import VirtualizedPosterGrid from '../../src/components/library/VirtualizedPosterGrid';
import { calculatePosterGridMetrics } from '../../src/components/library/posterGridMetrics';

// 探针：cleanDisplayTitle 在 PosterCard 每次实际渲染时都会执行。
// memo 化后，虚拟窗口平移时仍保持可见的卡片不应重复执行该渲染工作。
const cleanTitleCalls = vi.hoisted(() => new Map<string, number>());

vi.mock('../../src/utils/title', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/utils/title')>();
  return {
    ...mod,
    cleanDisplayTitle: (raw: string) => {
      cleanTitleCalls.set(raw, (cleanTitleCalls.get(raw) ?? 0) + 1);
      return mod.cleanDisplayTitle(raw);
    },
  };
});

function work(workId: string, title: string) {
  return {
    work_id: workId,
    title,
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
  cleanTitleCalls.clear();

  // 稳定的桌面尺寸：4 列海报网格，行高约 434px，视口 900px。
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    value: 900,
  });
  Object.defineProperty(HTMLElement.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({
      top: 0,
      left: 0,
      right: 1000,
      bottom: 0,
      width: 1000,
      height: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }),
  });
  if (typeof window.ResizeObserver === 'undefined') {
    (window as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

test('虚拟窗口平移一行时，保持可见的海报卡不重复执行渲染工作', async () => {
  const works = Array.from({ length: 30 }, (_, index) =>
    work(`w${index}`, `作品${index}`),
  );

  render(
    <main className="app-main">
      <VirtualizedPosterGrid works={works} columns={4} />
    </main>,
  );

  const main = document.querySelector<HTMLElement>('.app-main');
  expect(main).not.toBeNull();
  // 等挂载期的布局提交全部结束，记录「作品10」的渲染探针基线。
  await act(async () => {});
  const before = cleanTitleCalls.get('作品10') ?? 0;
  expect(before).toBeGreaterThan(0);

  // 向下滚动一行：startRow 从 0 变为 1，窗口内「作品10」保持可见。
  main!.scrollTop = 435;
  fireEvent.scroll(main!);
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });

  // memo 化前：网格重渲染会带动所有可见卡片重跑渲染函数 → 计数继续增长。
  // memo 化后：props 未变的卡片被跳过 → 计数保持不变。
  expect(cleanTitleCalls.get('作品10')).toBe(before);
});

test('首轮挂载到完成测量期间，不给图片设置原图地址', () => {
  const imageSources: string[] = [];
  const originalSetAttribute = Element.prototype.setAttribute;
  vi.spyOn(Element.prototype, 'setAttribute').mockImplementation(function (this: Element, name, value) {
    if (this.tagName === 'IMG' && name === 'src') imageSources.push(value);
    originalSetAttribute.call(this, name, value);
  });

  render(<VirtualizedPosterGrid works={[work('w1', '作品1')]} columns={4} />);

  expect(imageSources.length).toBeGreaterThan(0);
  expect(imageSources.every((src) => src.includes('/api/assets/thumbnail?'))).toBe(true);
});

test('宽度为零时只挂载可辨认卡片壳，恢复尺寸后才请求缩略图', async () => {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 0 });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 0, top: 0 } as DOMRect);
  render(<VirtualizedPosterGrid works={[work('w1', '作品1')]} columns={4} />);

  expect(screen.getByRole('button', { name: /作品1/ })).toBeTruthy();
  expect(screen.queryByRole('img')).toBeNull();

  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 1000 });
  fireEvent.resize(window);
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });
  expect(screen.getByRole('img')).toHaveAttribute('src', expect.stringContaining('/api/assets/thumbnail?'));
});

test('缓冲两行的挂载窗口提前加载图片且数量受窗口计算约束', async () => {
  const works = Array.from({ length: 200 }, (_, index) => work(`w${index}`, `作品${index}`));
  render(<main className="app-main"><VirtualizedPosterGrid works={works} columns={4} /></main>);
  const main = document.querySelector<HTMLElement>('.app-main')!;
  main.scrollTop = 2500;
  fireEvent.scroll(main);
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });
  const gap = Math.max(22, Math.min(32, window.innerWidth * 0.0155));
  const { rowHeight } = calculatePosterGridMetrics({ width: 1000, gap, requestedColumns: 4, imageMode: 'poster', metaHeight: 62 });
  const firstRow = Math.floor(2500 / rowHeight) - 2;
  const lastRow = Math.floor(2500 / rowHeight) + Math.ceil(900 / rowHeight) + 2;
  const images = screen.getAllByRole('img');
  expect(images).toHaveLength((lastRow - firstRow + 1) * 4);
  expect(images[0]).toHaveAttribute('alt', `作品${firstRow * 4}`);
  for (const image of images) {
    expect(image).toHaveAttribute('loading', 'eager');
    expect(image).toHaveAttribute('fetchpriority', 'auto');
  }
});

test('快速滚动在同一行内不提交网格更新，跨行时只更新一次窗口', async () => {
  const onRender = vi.fn();
  const works = Array.from({ length: 200 }, (_, index) => work(`w${index}`, `作品${index}`));
  render(<main className="app-main"><Profiler id="grid" onRender={onRender}>
    <VirtualizedPosterGrid works={works} columns={4} />
  </Profiler></main>);
  await act(async () => {});
  onRender.mockClear();
  const main = document.querySelector<HTMLElement>('.app-main')!;
  const gap = Math.max(22, Math.min(32, window.innerWidth * 0.0155));
  const { rowHeight } = calculatePosterGridMetrics({ width: 1000, gap, requestedColumns: 4, imageMode: 'poster', metaHeight: 62 });
  // 旧逻辑在视口底部跨行和顶部跨行时分别更新，同一行末端也会额外挂载图片。
  for (const fraction of [0.4, 0.8, 0.99]) {
    main.scrollTop = rowHeight * fraction;
    fireEvent.scroll(main);
    await act(async () => { await new Promise<void>((resolve) => requestAnimationFrame(() => resolve())); });
  }
  expect(onRender).not.toHaveBeenCalled();
  main.scrollTop = rowHeight * 1.01;
  fireEvent.scroll(main);
  fireEvent.scroll(main);
  await act(async () => { await new Promise<void>((resolve) => requestAnimationFrame(() => resolve())); });
  expect(onRender).toHaveBeenCalledTimes(1);
});
