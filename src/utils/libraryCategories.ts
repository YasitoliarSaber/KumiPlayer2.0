import type { WorkIndex } from '../api/types';
import type { LibraryView, SourceId } from '../stores/ui';

export type CategoryWorkCounts = Record<LibraryView, number>;

export function isSeasonalWork(work: WorkIndex): boolean {
  // 显式来源范围优先；未声明范围的历史作品兼容既有观看分类。
  if (work.content_scope) return work.content_scope === 'ongoing';
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
