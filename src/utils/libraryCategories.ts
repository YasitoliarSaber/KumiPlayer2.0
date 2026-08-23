import type { WorkIndex } from '../api/types';
import type { LibraryView, SourceId } from '../stores/ui';

export type CategoryWorkCounts = Record<LibraryView, number>;

export function isSeasonalWork(work: WorkIndex): boolean {
  // V4 不再从追更绑定或导入计划推导分类。季度追更入口使用媒体管理
  // 的统一 revision 流程，媒体库卡片只表达已确认的作品图。
  void work;
  return false;
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
