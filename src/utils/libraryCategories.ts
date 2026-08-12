import type { WorkIndex } from '../api/types';
import type { LibraryView, SourceId } from '../stores/ui';

export type CategoryWorkCounts = Record<LibraryView, number>;

export function isSeasonalWork(work: WorkIndex): boolean {
  const trackingState = work.tracking?.tracking_state;
  if (trackingState === 'completed' || trackingState === 'archived') return false;
  if (trackingState === 'tracking' || trackingState === 'paused') return true;
  return work.import_scope === 'seasonal';
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
