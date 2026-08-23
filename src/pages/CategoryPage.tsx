import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { useLibraryStore } from '../stores/library';
import { useUiStore, type LibraryView, type SortId } from '../stores/ui';
import { matchesSourceFilter } from '../utils/sourceFilter';
import { getSortDimension, getSortOption, sortDimensions, toggleSort } from '../utils/categorySort';
import { useDismissiblePopover } from '../hooks/useDismissiblePopover';
import VirtualizedPosterGrid from '../components/library/VirtualizedPosterGrid';
import LibraryViewControls, { normalizeColumns } from '../components/library/LibraryViewControls';
import LoadingState from '../components/ui/loading-state';
import { isWorkInLibraryView } from '../utils/libraryCategories';

const categoryLabels: Record<LibraryView, string> = {
  seasonal: '新番',
  anime_series: '番剧',
  anime_movie: '动画电影',
  live_series: '剧集',
  live_movie: '电影',
};

export default function CategoryPage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const activeCategory = useUiStore((state) => state.activeCategory);
  const source = useUiStore((state) => state.source);
  const sort = useUiStore((state) => state.sort);
  const setSort = useUiStore((state) => state.setSort);
  const posterSize = useUiStore((state) => state.posterSize);
  const [sortOpen, setSortOpen] = useState(false);
  const [columnCapacity, setColumnCapacity] = useState<number>();

  useLayoutEffect(() => {
    if (!activeCategory) return;
    const main = document.querySelector<HTMLElement>('.app-main');
    const frame = requestAnimationFrame(() => {
      const restored = useUiStore.getState().consumeCategoryScrollRestore(activeCategory, source);
      main?.scrollTo({ top: restored ?? 0, behavior: 'auto' });
    });
    return () => cancelAnimationFrame(frame);
  }, [activeCategory, source]);

  const categoryWorks = useMemo(() => {
    if (!activeCategory) return [];
    const filtered = works
      .filter((work) => isWorkInLibraryView(work, activeCategory))
      .filter((work) => source === 'all' || matchesSourceFilter(work, source));
    const sorted = [...filtered];
    const recentRank = new Map(history.map((item, index) => [item.work_id, index]));
    switch (sort) {
      case 'recent':
        sorted.sort((left, right) => (recentRank.get(left.work_id) ?? Number.MAX_SAFE_INTEGER) - (recentRank.get(right.work_id) ?? Number.MAX_SAFE_INTEGER));
        break;
      case 'title':
        sorted.sort((left, right) => left.title.localeCompare(right.title, 'zh-Hans-CN'));
        break;
      case 'titleDesc':
        sorted.sort((left, right) => right.title.localeCompare(left.title, 'zh-Hans-CN'));
        break;
      case 'yearAsc':
        sorted.sort((left, right) => (left.year ?? 9999) - (right.year ?? 9999));
        break;
      case 'year':
      case 'yearDesc':
        sorted.sort((left, right) => (right.year || 0) - (left.year || 0));
        break;
      case 'ratingAsc':
        sorted.sort((left, right) => (left.rating || 0) - (right.rating || 0));
        break;
      case 'rating':
      case 'ratingDesc':
        sorted.sort((left, right) => (right.rating || 0) - (left.rating || 0));
        break;
      case 'episodesAsc':
        sorted.sort((left, right) => (left.episode_count || 0) - (right.episode_count || 0));
        break;
      case 'episodesDesc':
        sorted.sort((left, right) => (right.episode_count || 0) - (left.episode_count || 0));
        break;
      default:
        break;
    }
    return sorted;
  }, [activeCategory, history, sort, source, works]);

  if (loading && works.length === 0) return <LoadingState label="正在载入分类" detail="正在读取 V4 媒体库投影" />;
  if (error && works.length === 0) return <CenteredMessage>{error}</CenteredMessage>;
  if (!activeCategory) return <CenteredMessage>未选择分类</CenteredMessage>;

  return (
    <div className="category-page">
      <div className="category-head">
        <div className="category-title-block"><h1>{categoryLabels[activeCategory]}</h1><span>共 {categoryWorks.length} 部</span></div>
        <div className="category-toolbar" role="toolbar" aria-label="分类视图工具">
          {activeCategory === 'seasonal' && <span className="category-v4-note">导入和扫描统一在媒体管理中进行</span>}
          <SortMenu value={sort} open={sortOpen} onOpenChange={setSortOpen} onChange={setSort} />
          <LibraryViewControls maxColumns={columnCapacity} />
        </div>
      </div>
      {categoryWorks.length === 0 ? <CenteredMessage>这个筛选下还没有作品</CenteredMessage> : (
        <div className="category-grid-wrap">
          <VirtualizedPosterGrid works={categoryWorks} columns={normalizeColumns(posterSize)} onColumnCapacityChange={setColumnCapacity} localArtworkOnly />
        </div>
      )}
    </div>
  );
}

function SortMenu({
  value,
  open,
  onOpenChange,
  onChange,
}: {
  value: SortId;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChange: (value: SortId) => void;
}) {
  const current = getSortOption(value);
  const menuRef = useRef<HTMLDivElement>(null);
  const selectSortDimension = (dimension: typeof sortDimensions[number]) => onChange(toggleSort(value, dimension));
  useDismissiblePopover(open, () => onOpenChange(false), menuRef);

  return (
    <div className="sort-menu-wrap" ref={menuRef}>
      <button className={`sort-trigger ${open ? 'active' : ''}`} onClick={() => onOpenChange(!open)} title="排序" aria-haspopup="menu" aria-expanded={open}>
        <span>{current.directionLabel ? `${current.label} ${current.directionLabel}` : current.label}</span><ChevronDown size={15} strokeWidth={1.8} />
      </button>
      {open && <div className="sort-menu" role="menu" aria-label="排序方式">
        {sortDimensions.map((dimension) => {
          const option = getSortOption(dimension === 'recent' ? 'recent' : toggleSort('recent', dimension));
          const active = getSortDimension(value) === dimension;
          return <button key={dimension} className={active ? 'active' : ''} role="menuitemradio" aria-checked={active} onClick={() => selectSortDimension(dimension)}><span>{option.label}</span>{active && current.directionLabel && <span>{current.directionLabel}</span>}</button>;
        })}
      </div>}
    </div>
  );
}

function CenteredMessage({ children }: { children: string }) {
  return <div className="page-loading-wrap"><div className="page-loading-message">{children}</div></div>;
}
