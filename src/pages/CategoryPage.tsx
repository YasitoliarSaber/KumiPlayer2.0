import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@fluentui/react-components';
import { ChevronDown, RefreshCw } from 'lucide-react';
import { useLibraryStore } from '../stores/library';
import { useUiStore, type LibraryView, type SortId } from '../stores/ui';
import { matchesSourceFilter } from '../utils/sourceFilter';
import { getSortDimension, getSortOption, sortDimensions, toggleSort } from '../utils/categorySort';
import { useDismissiblePopover } from '../hooks/useDismissiblePopover';
import { mediaV4Api, type V4TrackingSource } from '../api/mediaV4';
import { queueOngoingSourceAction } from '../components/media/ongoingSourceNavigation';
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

const trackingStatusLabels: Record<string, string> = {
  idle: '尚未更新', queued: '等待更新', running: '正在更新',
  needs_confirmation: '待人工确认', needs_new_txt: '需要新 TXT',
  failed: '更新失败', completed: '更新完成', cancelled: '已取消',
  cancelling: '正在取消', blocked: '需要处理', paused: '已暂停', interrupted: '更新中断',
};

function activeTrackingSource(source: V4TrackingSource) {
  return ['queued', 'running', 'cancelling'].includes(source.status);
}

export default function CategoryPage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const activeCategory = useUiStore((state) => state.activeCategory);
  const ongoingCategoryName = useUiStore((state) => state.ongoingCategoryName) || '新番';
  const source = useUiStore((state) => state.source);
  const sort = useUiStore((state) => state.sort);
  const setSort = useUiStore((state) => state.setSort);
  const goManageView = useUiStore((state) => state.goManageView);
  const posterSize = useUiStore((state) => state.posterSize);
  const [sortOpen, setSortOpen] = useState(false);
  const [columnCapacity, setColumnCapacity] = useState<number>();
  const [scanTasks, setScanTasks] = useState<V4TrackingSource[]>([]);
  const [scanning, setScanning] = useState(false);
  const [scanMessage, setScanMessage] = useState('');
  const scanRequest = useRef(0);

  // 页面只观察既有任务；进入页面与状态轮询均不触发来源扫描。
  useEffect(() => {
    if (activeCategory !== 'seasonal') {
      setScanning(false);
      return;
    }
    let cancelled = false;
    const request = ++scanRequest.current;
    void mediaV4Api.trackingSources().then((result) => {
      if (!cancelled && request === scanRequest.current) setScanTasks(result.sources);
    }).catch((cause) => {
      if (!cancelled && request === scanRequest.current) setScanMessage(cause instanceof Error ? cause.message : '无法读取新番更新状态');
    });
    return () => { cancelled = true; ++scanRequest.current; };
  }, [activeCategory]);

  useEffect(() => {
    if (activeCategory !== 'seasonal' || scanning) return;
    let cancelled = false;
    const request = scanRequest.current;
    let timer = 0;
    const poll = async () => {
      let delay = 5000;
      try {
        const result = await mediaV4Api.trackingSources();
        if (cancelled || request !== scanRequest.current) return;
        setScanTasks(result.sources);
        if (result.sources.some(activeTrackingSource)) delay = 1500;
      } catch (cause) {
        if (cancelled || request !== scanRequest.current) return;
        setScanMessage(cause instanceof Error ? cause.message : '无法读取新番更新状态，请刷新重试');
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), delay);
    };
    timer = window.setTimeout(() => void poll(), scanTasks.some(activeTrackingSource) ? 1500 : 5000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [activeCategory, scanTasks, scanning]);

  const startSeasonalScan = useCallback(async () => {
    const request = ++scanRequest.current;
    setScanning(true);
    setScanMessage('');
    try {
      const result = await mediaV4Api.trackingScanAll();
      if (request !== scanRequest.current) return;
      setScanTasks(result.tasks);
      setScanMessage(result.tasks.length ? '已检查新番来源，请查看各来源的更新状态。' : '还没有新番来源，请在媒体管理导入时选择“新番”。');
    } catch (cause) {
      if (request === scanRequest.current) setScanMessage(cause instanceof Error ? cause.message : '发起新番更新失败');
    } finally {
      if (request === scanRequest.current) setScanning(false);
    }
  }, []);

  const cancelSeasonalScan = useCallback(async (scanId: string) => {
    try {
      await mediaV4Api.trackingCancelScan(scanId);
      setScanTasks((current) => current.map((task) => task.task_id === scanId ? { ...task, status: 'cancelling' } : task));
      setScanMessage('已请求取消更新。');
    } catch (cause) {
      setScanMessage(cause instanceof Error ? cause.message : '取消更新失败');
    }
  }, []);

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
        <div className="category-title-block"><h1>{activeCategory === 'seasonal' ? ongoingCategoryName : categoryLabels[activeCategory]}</h1><span>共 {categoryWorks.length} 部</span></div>
        <div className="category-toolbar" role="toolbar" aria-label="分类视图工具">
          {activeCategory === 'seasonal' && (
            <div className="category-seasonal-actions">
              <Button className="category-scan-button" icon={<RefreshCw size={16} />} aria-label={`${scanning ? '正在刷新' : '刷新'}${ongoingCategoryName}`} disabled={scanning} onClick={() => void startSeasonalScan()}>
                {scanning ? '正在发起…' : `刷新${ongoingCategoryName}`}
              </Button>
              {scanTasks.some((task) => task.task_id && ['queued', 'running'].includes(task.status)) && <Button className="category-scan-cancel" onClick={() => scanTasks.filter((task) => task.task_id && ['queued', 'running'].includes(task.status)).forEach((task) => void cancelSeasonalScan(task.task_id!))}>取消更新</Button>}
            </div>
          )}
          <SortMenu value={sort} open={sortOpen} onOpenChange={setSortOpen} onChange={setSort} />
          <LibraryViewControls maxColumns={columnCapacity} />
        </div>
      </div>
      {activeCategory === 'seasonal' && <>
        {scanMessage && <div className="category-scan-message" role="status">{scanMessage}</div>}
        {scanTasks.length > 0 && <section aria-label={`${ongoingCategoryName}来源更新`}>
          {scanTasks.map((task) => <div className="category-scan-message" key={task.root_id} role="group" aria-label={task.display_name || '媒体来源'}>
            <strong>{task.display_name || '媒体来源'}</strong>{' · '}
            <span role="status">{trackingStatusLabels[task.status] || '需要处理'}</span>
            {task.reason && <span> · {task.reason}</span>}
            {task.status === 'needs_confirmation' && <Button appearance="subtle" onClick={() => {
              queueOngoingSourceAction({ root_id: task.root_id, action: 'review', ...(task.draft_revision_id ? { revision_id: task.draft_revision_id } : {}) });
              goManageView('overview');
            }}>查看识别结果</Button>}
            {task.status === 'needs_new_txt' && <Button appearance="subtle" onClick={() => {
              queueOngoingSourceAction({ root_id: task.root_id, action: 'new_txt' });
              goManageView('overview');
            }}>选择新导出 TXT</Button>}
          </div>)}
        </section>}
      </>}
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
