import type { WorkIndex } from '../api/types';
import type { LibraryView, SourceId } from '../stores/ui';

export type CategoryWorkCounts = Record<LibraryView, number>;

export function isSeasonalWork(work: WorkIndex): boolean {
  // P-006：追更分类基于后端 watch_status（tracking_states 权威），不再常量 false；
  // 前端不做标题/路径正则推导。
  return work.watch_status?.status === 'watching' || work.watch_status?.status === 'on_hold';
}

export function isWorkInLibraryView(work: WorkIndex, view: LibraryView): boolean {
  if (view === 'seasonal') return isSeasonalWork(work);
  if (view === 'anime_series') {
    return work.show_type === 'anime_series' && !isSeasonalWork(work);
  }
  return work.show_type === view;
}

export function categoryWorkCounts(works: WorkIndex[], source: SourceId): CategoryWorkCounts {
  const visibleWorks = source === 'all'
    ? works
    : works.filter((work) => (work.sources || [work.source]).includes(source));
  const counts: CategoryWorkCounts = {
    seasonal: 0,
    anime_series: 0,
    anime_movie: 0,
    live_series: 0,
    live_movie: 0,
  };

  for (const work of visibleWorks) {
    for (const view of Object.keys(counts) as LibraryView[]) {
      if (isWorkInLibraryView(work, view)) counts[view] += 1;
    }
  }
  return counts;
}
