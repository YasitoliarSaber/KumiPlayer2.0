/**
 * 「最近观看」卡片结构合同：继续播放入口，图片承担第一视觉焦点，文字只有三层，
 * 进度只由图片底部的进度条表达（不重复输出精确位置/总时长/百分比文本）。
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import RecentWatchCard from '../../src/components/library/RecentWatchCard';
import type { PlaybackHistoryItem, WorkIndex } from '../../src/api/types';

const openWorkDetail = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/assets', () => ({
  buildAssetUrl: (path: string) => (path ? `/assets/${path}` : ''),
}));
vi.mock('../../src/stores/library', () => ({
  useLibraryStore: (selector: (state: unknown) => unknown) => selector({ openWorkDetail }),
}));
vi.mock('../../src/components/ui/DecodedImage', () => ({
  default: ({ src, alt, className }: { src: string; alt: string; className?: string }) => (
    <img className={className} src={src} alt={alt} />
  ),
}));

function work(overrides: Partial<WorkIndex> = {}): WorkIndex {
  return {
    work_id: 'w1',
    title: '斩服少女',
    original_title: '',
    year: 2013,
    rating: 0,
    plot: '',
    genres: [],
    studios: [],
    show_type: 'anime_series',
    media_type: 'tv',
    source: 'local',
    card_type: 'main_series',
    poster_path: '',
    fanart_path: '',
    clearlogo_path: '',
    dir_path: '',
    ...overrides,
  } as WorkIndex;
}

function history(overrides: Partial<PlaybackHistoryItem> = {}): PlaybackHistoryItem {
  return {
    work_id: 'w1',
    episode_id: 'e1',
    asset_id: 'a1',
    position: 76,
    duration: 1468,
    completed: false,
    updated_at: '2026-10-01T12:50:00',
    season_number: 1,
    episode_number: 1,
    thumb_path: 'mirror/thumb.jpg',
    ...overrides,
  } as PlaybackHistoryItem;
}

function renderCard(props: Partial<React.ComponentProps<typeof RecentWatchCard>> = {}) {
  return render(
    <RecentWatchCard
      work={work()}
      history={history()}
      episodeLabel="S01E01"
      statusLabel="还剩 23 分钟"
      timeLabel="今天 12:50"
      {...props}
    />,
  );
}

describe('「最近观看」卡片', () => {
  it('只渲染三层文字：作品名、集号·剩余时间、时间', () => {
    const { container } = renderCard();
    const copy = container.querySelector('.recent-watch-copy');
    expect(copy).not.toBeNull();
    expect(copy!.children).toHaveLength(3);
    expect(copy!.children[0]).toHaveClass('recent-watch-title');
    expect(copy!.children[1]).toHaveClass('recent-watch-position');
    expect(copy!.children[2]).toHaveClass('recent-watch-time');
    // 三层文字内容固定；图片占位文案在媒体层里，且对无障碍隐藏。
    expect(copy!.textContent).toBe('斩服少女S01E01·还剩 23 分钟今天 12:50');
    expect(container.querySelector('.recent-watch-placeholder')!.getAttribute('aria-hidden')).toBe('true');
  });

  it('不出现位置/总时长/百分比这类调试文案', () => {
    const { container } = renderCard();
    const text = container.textContent || '';
    expect(text).not.toMatch(/\d+:\d+\s*\/\s*\d+:\d+/);
    expect(text).not.toContain('%');
    expect(text).not.toMatch(/已看完\s*$/);
  });

  it('进度只由图片底部进度条表达，已看完时铺满', () => {
    const { container, unmount } = renderCard({ history: history({ position: 734, duration: 1468 }) });
    const bar = container.querySelector('.recent-watch-progress > span');
    expect(bar).not.toBeNull();
    expect((bar as HTMLElement).style.width).toBe('50%');
    // 进度条本身是装饰，不额外占用无障碍语义。
    expect(container.querySelector('.recent-watch-progress')!.getAttribute('aria-hidden')).toBe('true');
    unmount();

    const done = renderCard({ history: history({ completed: true }), statusLabel: '已看完 ✓' });
    const doneBar = done.container.querySelector('.recent-watch-progress > span');
    expect((doneBar as HTMLElement).style.width).toBe('100%');
    expect(done.container.textContent).toContain('已看完 ✓');
  });

  it('无进度时不渲染进度条，也不编造百分比', () => {
    const { container } = renderCard({ history: history({ position: 0, duration: 0 }), statusLabel: '继续观看' });
    expect(container.querySelector('.recent-watch-progress')).toBeNull();
    expect(container.textContent).toContain('继续观看');
    expect(container.textContent).not.toContain('%');
  });

  it('电影用「电影 · 还剩 …」而不是季集号', () => {
    const { container } = renderCard({
      work: work({ media_type: 'movie', title: '摇曳露营△ 剧场版' }),
      history: history({ season_number: null, episode_number: null }),
      episodeLabel: '电影',
      statusLabel: '还剩 1 小时 14 分',
      timeLabel: '昨天 22:49',
    });
    expect(within(container).getByText('摇曳露营△ 剧场版')).toBeTruthy();
    expect(container.querySelector('.recent-watch-position')!.textContent).toBe('电影·还剩 1 小时 14 分');
    expect(container.textContent).not.toMatch(/S\d{2}E\d{2}/);
  });

  it('整张卡片是一个可聚焦的继续播放入口，无障碍名称含状态与时间', () => {
    renderCard();
    const button = screen.getByRole('button', { name: '斩服少女 · S01E01 · 还剩 23 分钟 · 今天 12:50' });
    expect(button).toHaveClass('recent-watch-card');
  });
});
