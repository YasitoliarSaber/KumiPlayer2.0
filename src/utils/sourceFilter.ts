import type { WorkIndex } from '../api/types';
import type { SourceId } from '../stores/ui';

/**
 * 来源筛选统一语义：作品主来源或多来源列表命中即保留。
 *
 * 多来源卡片（source=pan115 且 sources 含 baidu）在任何命中来源下都必须
 * 可见；此前该语义在 store/CategoryPage 正确，而搜索/首页/最近/收藏页只判
 * 主来源导致作品丢失——所有页面一律引用本函数，不再各自内联。
 */
export function matchesSourceFilter(
  work: Pick<WorkIndex, 'source' | 'sources'>,
  source: SourceId,
): boolean {
  if (source === 'all') return true;
  return work.source === source || (work.sources || []).includes(source);
}
