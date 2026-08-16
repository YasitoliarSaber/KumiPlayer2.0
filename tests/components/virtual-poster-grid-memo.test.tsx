import { act, fireEvent, render } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import VirtualizedPosterGrid from '../../src/components/library/VirtualizedPosterGrid';

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
