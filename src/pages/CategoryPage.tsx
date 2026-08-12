import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, Cloud, FolderOpen, HardDrive, Plus, ScanLine, Square, X } from 'lucide-react';
import { useLibraryStore } from '../stores/library';
import { useUiStore, type LibraryView, type SortId } from '../stores/ui';
import { getSortDimension, getSortOption, sortDimensions, toggleSort } from '../utils/categorySort';
import { useDismissiblePopover } from '../hooks/useDismissiblePopover';
import VirtualizedPosterGrid from '../components/library/VirtualizedPosterGrid';
import LibraryViewControls, { normalizeColumns } from '../components/library/LibraryViewControls';
import LoadingState from '../components/ui/loading-state';
import { trackingApi } from '../api/tracking';
import type { TaskRecord, TrackingBinding } from '../api/types';
import { tasksApi } from '../api/tasks';
import { isWorkInLibraryView } from '../utils/libraryCategories';
import { pickFolder } from '../platform/folderPicker';
import { formatTrackingScanNotice } from '../utils/trackingScanSummary';

const categoryLabels: Record<LibraryView, string> = {
  seasonal: '新番',
  anime_series: '番剧',
  anime_movie: '动画电影',
  live_series: '剧集',
  live_movie: '电影',
};

const seasonalSourceOptions = [
  { value: 'local', label: '本地文件夹', description: '电脑硬盘或局域网目录', icon: HardDrive },
  { value: 'pan115', label: '115 挂载', description: '已挂载到电脑的 115 目录', icon: Cloud },
  { value: 'baidu', label: '百度挂载', description: '已挂载到电脑的百度目录', icon: Cloud },
] as const;

type SeasonalTaskLog = { kind: 'info' | 'warn' | 'error'; message: string };

function taskResultRecord(task: TaskRecord | null): Record<string, unknown> {
  return task?.result && typeof task.result === 'object'
    ? task.result as Record<string, unknown>
    : {};
}

function seasonalTaskLogs(task: TaskRecord | null): SeasonalTaskLog[] {
  const result = taskResultRecord(task);
  const rawLogs = Array.isArray(result.logs) ? result.logs : [];
  const logs = rawLogs.flatMap((entry): SeasonalTaskLog[] => {
    if (!entry || typeof entry !== 'object') return [];
    const record = entry as Record<string, unknown>;
    const message = String(record.message || '').trim();
    if (!message) return [];
    const rawKind = String(record.kind || 'info');
    const kind = rawKind === 'error' ? 'error' : rawKind === 'warn' ? 'warn' : 'info';
    return [{ kind, message }];
  });
  if (!logs.length && task?.message) logs.push({ kind: task.status === 'failed' ? 'error' : 'info', message: task.message });
  return logs;
}

export default function CategoryPage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const {
    activeCategory,
    source,
    sort,
    setSort,
    posterSize,
  } = useUiStore();
  const [sortOpen, setSortOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [activeScanTaskId, setActiveScanTaskId] = useState('');
  const [latestAddTask, setLatestAddTask] = useState<TaskRecord | null>(null);
  const [stoppingScan, setStoppingScan] = useState(false);
  const [notice, setNotice] = useState('');
  const [trackingBindings, setTrackingBindings] = useState<TrackingBinding[]>([]);
  const [columnCapacity, setColumnCapacity] = useState<number>();
  const [addMode, setAddMode] = useState<'root' | 'single'>('root');
  const recoveredScanTaskIdRef = useRef('');
  const seasonalTaskLogRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState({ title: '', path: '', season: '1', source: 'local' as 'local' | 'pan115' | 'baidu' });

  const loadTrackingBindings = async () => {
    try {
      const response = await trackingApi.list();
      setTrackingBindings(response.items);
    } catch {
      setTrackingBindings([]);
    }
  };

  useEffect(() => {
    if (activeCategory === 'seasonal') void loadTrackingBindings();
  }, [activeCategory]);

  useLayoutEffect(() => {
    if (!activeCategory) return;
    const main = document.querySelector<HTMLElement>('.app-main');
    // 消费必须发生在 rAF 回调内：React Strict Mode 在开发态会先运行 effect 再 cleanup，
    // 若在 effect 同步阶段消费，第一次 cleanup 取消 rAF 时 restore 已丢失，第二次 effect 就会置顶。
    const frame = requestAnimationFrame(() => {
      const restoredScrollTop = useUiStore.getState().consumeCategoryScrollRestore(activeCategory, source);
      main?.scrollTo({ top: restoredScrollTop ?? 0, behavior: 'auto' });
    });
    return () => cancelAnimationFrame(frame);
  }, [activeCategory, source]);

  const categoryWorks = useMemo(() => {
    if (!activeCategory) return [];

    let filtered = works.filter((work) => isWorkInLibraryView(work, activeCategory));
    if (source !== 'all') filtered = filtered.filter((work) => (work.sources || [work.source]).includes(source));

    const sorted = [...filtered];
    const recentRank = new Map(history.map((item, index) => [item.work_id, index]));
    switch (sort) {
      case 'recent':
        sorted.sort((a, b) => {
          const aRank = recentRank.get(a.work_id) ?? Number.MAX_SAFE_INTEGER;
          const bRank = recentRank.get(b.work_id) ?? Number.MAX_SAFE_INTEGER;
          if (aRank !== bRank) return aRank - bRank;
          return a.title.localeCompare(b.title, 'zh-Hans-CN');
        });
        break;
      case 'title':
        sorted.sort((a, b) => a.title.localeCompare(b.title, 'zh-Hans-CN'));
        break;
      case 'titleDesc':
        sorted.sort((a, b) => b.title.localeCompare(a.title, 'zh-Hans-CN'));
        break;
      case 'rating':
      case 'ratingDesc':
        sorted.sort((a, b) => (b.rating || 0) - (a.rating || 0));
        break;
      case 'ratingAsc':
        sorted.sort((a, b) => (a.rating || 0) - (b.rating || 0));
        break;
      case 'year':
      case 'yearDesc':
        sorted.sort((a, b) => (b.year || 0) - (a.year || 0));
        break;
      case 'yearAsc':
        sorted.sort((a, b) => (a.year ?? 9999) - (b.year ?? 9999));
        break;
      default:
        break;
    }

    return sorted;
  }, [works, history, activeCategory, source, sort]);

  const visibleSeasonalWorkIds = useMemo(
    () => new Set(categoryWorks.map((work) => work.work_id)),
    [categoryWorks],
  );

  const scannableBindings = useMemo(() => trackingBindings.filter((binding) => (
    binding.tracking_state === 'tracking'
    && visibleSeasonalWorkIds.has(binding.work_id)
    && (source === 'all' || binding.logical_source === source)
  )), [trackingBindings, visibleSeasonalWorkIds, source]);

  const sourcePathCopy = useMemo(() => {
    const modeLabel = addMode === 'root' ? '媒体库根目录' : '作品目录';
    if (draft.source === 'pan115') {
      return {
        label: `115 挂载${modeLabel}`,
        placeholder: addMode === 'root' ? '选择包含多部作品的 115 挂载根目录' : '选择这部作品的 115 挂载目录',
      };
    }
    if (draft.source === 'baidu') {
      return {
        label: `百度挂载${modeLabel}`,
        placeholder: addMode === 'root' ? '选择包含多部作品的百度挂载根目录' : '选择这部作品的百度挂载目录',
      };
    }
    return {
      label: `本地${modeLabel}`,
      placeholder: addMode === 'root' ? '选择包含多部作品文件夹的本地根目录' : '选择这部作品的本地目录',
    };
  }, [addMode, draft.source]);

  const addTaskResult = useMemo(() => taskResultRecord(latestAddTask), [latestAddTask]);
  const addTaskLogs = useMemo(() => seasonalTaskLogs(latestAddTask), [latestAddTask]);
  const addTaskInFlight = Boolean(activeScanTaskId) || latestAddTask?.status === 'pending' || latestAddTask?.status === 'running';
  const addTaskProgress = Math.max(0, Math.min(100, Number(latestAddTask?.progress || 0)));
  const detectedWorkCount = Number(addTaskResult.detected_work_count || 0);
  const addTaskVisualStatus = addTaskResult.status === 'blocked'
    ? 'blocked'
    : latestAddTask?.status || 'idle';
  const addTaskStatusLabel = addTaskVisualStatus === 'blocked'
    ? '需要处理'
    : latestAddTask?.status === 'succeeded'
      ? '已完成'
    : latestAddTask?.status === 'failed'
      ? '处理失败'
      : latestAddTask?.status === 'pending'
        ? '等待处理'
        : latestAddTask?.status === 'running'
          ? '正在处理'
          : '尚未开始';
  const addTaskCurrentTarget = String(addTaskResult.current_target || latestAddTask?.message || '提交后将在这里显示实时进度');

  useEffect(() => {
    const log = seasonalTaskLogRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [latestAddTask?.task_id, latestAddTask?.progress, addTaskLogs.length]);

  useEffect(() => {
    if (!addOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setAddOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [addOpen]);

  const waitForTask = async (taskId: string) => {
    for (let index = 0; index < 120; index += 1) {
      const task = await tasksApi.get(taskId);
      setLatestAddTask(task);
      setNotice(task.message || '正在处理');
      if (task.status === 'succeeded') return task;
      if (task.status === 'failed') throw new Error(task.error || '任务失败');
      await new Promise((resolve) => window.setTimeout(resolve, 750));
    }
    throw new Error('任务仍在后台运行，请稍后刷新');
  };

  useEffect(() => {
    if (activeCategory !== 'seasonal') return;
    let disposed = false;
    let timer: number | undefined;

    const recoverActiveTrackingTask = async () => {
      try {
        const response = await tasksApi.list({ type_prefix: 'tracking_', limit: 20 });
        const latestImportTask = response.tasks.find((task) => task.task_type === 'tracking_import_root');
        const active = response.tasks.find((task) => (
          (task.status === 'pending' || task.status === 'running')
          && (source === 'all' || task.source === source || task.source === 'all')
        ));
        if (disposed) return;
        if (active) {
          recoveredScanTaskIdRef.current = active.task_id;
          setActiveScanTaskId(active.task_id);
          setLatestAddTask(active);
          setNotice(active.message || '新番任务正在后台运行');
        } else if (recoveredScanTaskIdRef.current) {
          const finished = response.tasks.find((task) => task.task_id === recoveredScanTaskIdRef.current);
          if (finished) setLatestAddTask(finished);
          recoveredScanTaskIdRef.current = '';
          setActiveScanTaskId('');
          setStoppingScan(false);
          await Promise.all([loadLibrary({ force: true }), loadTrackingBindings()]);
          if (!disposed) {
            const result = taskResultRecord(finished || null);
            setNotice(finished?.task_type === 'tracking_scan_all'
              ? formatTrackingScanNotice(result)
              : result.status === 'blocked'
                ? String(result.error || '后台任务已结束，部分作品需要人工确认')
                : '后台新番任务已结束，媒体库已刷新');
          }
        } else if (latestImportTask) {
          setLatestAddTask((current) => (
            !current || latestImportTask.created_at > current.created_at ? latestImportTask : current
          ));
        }
      } catch {
        // 后端短暂不可用时保留当前任务按钮，下次轮询继续恢复。
      } finally {
        if (!disposed) timer = window.setTimeout(recoverActiveTrackingTask, 1500);
      }
    };

    void recoverActiveTrackingTask();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [activeCategory, source]);

  const scanAllTracking = async () => {
    setBusy(true); setNotice('开始扫描并自动更新追更目录');
    try {
      const task = await trackingApi.scanAll({
        includeScrape: true,
        // 新番追更暂不支持 OpenList：该来源没有可扫描绑定，按钮已禁用；
        // 这里兜底为全量来源避免向后端发送不支持的来源值。
        source: source === 'openlist' ? 'all' : source,
        workIds: scannableBindings.map((binding) => binding.work_id),
      });
      setActiveScanTaskId(task.task_id);
      const completedTask = await waitForTask(task.task_id);
      await loadLibrary({ force: true });
      await loadTrackingBindings();
      const result = (completedTask.result || {}) as Record<string, unknown>;
      setNotice(formatTrackingScanNotice(result));
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); setActiveScanTaskId(''); setStoppingScan(false); }
  };

  const stopScan = async () => {
    if (!activeScanTaskId || stoppingScan) return;
    setStoppingScan(true);
    setNotice('正在停止扫描，当前安全步骤结束后将不再处理后续作品');
    try {
      await tasksApi.cancel(activeScanTaskId);
    } catch (error) {
      setStoppingScan(false);
      setNotice(`停止失败：${(error as Error).message}`);
    }
  };

  const addTracking = async () => {
    if (!draft.path.trim() || addTaskInFlight) return;
    setBusy(true);
    setNotice(addMode === 'root' ? '正在识别新番媒体库中的全部作品' : '创建追更作品');
    try {
      const task = addMode === 'root'
        ? await trackingApi.importRoot({ rootPath: draft.path.trim(), source: draft.source, includeScrape: true })
        : await trackingApi.scan((await trackingApi.create({
            display_title: draft.title.trim(), root_path: draft.path.trim(), logical_source: draft.source,
            season_number: Number(draft.season) || 1,
          })).work_id, true);
      setActiveScanTaskId(task.task_id);
      recoveredScanTaskIdRef.current = task.task_id;
      setLatestAddTask(await tasksApi.get(task.task_id));
      setNotice('任务已在后台处理，可以关闭弹窗后继续使用媒体库');
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); setStoppingScan(false); }
  };

  const chooseTrackingFolder = async () => {
    const selected = await pickFolder(draft.path, '选择新番作品目录');
    if (!selected) return;
    const folderName = selected.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) || '';
    const inferredTitle = folderName.replace(/\s*[（(]\d{4}[）)]\s*$/, '').trim();
    setDraft((current) => ({ ...current, path: selected, title: current.title || inferredTitle }));
  };

  if (loading && works.length === 0) return <LoadingState label="正在载入分类" detail="正在整理作品列表" />;
  if (error && works.length === 0) return <CenteredMessage>{error}</CenteredMessage>;
  if (!activeCategory) return <CenteredMessage>未选择分类</CenteredMessage>;

  const columnsPerRow = normalizeColumns(posterSize);

  return (
    <div className="category-page">
      <div className="category-head">
        <div className="category-title-block">
          <h1>
            {categoryLabels[activeCategory]}
          </h1>
          <span>
            共 {categoryWorks.length} 部
          </span>
        </div>

        <div className="category-toolbar" role="toolbar" aria-label="分类视图工具">
          {activeCategory === 'seasonal' && <div className="category-toolbar-group seasonal-actions">
            <button className="seasonal-command primary" disabled={busy && !activeScanTaskId} onClick={() => setAddOpen(true)}>{activeScanTaskId ? <><ScanLine size={16} />查看进度</> : <><Plus size={16} />新增新番</>}</button>
            {activeScanTaskId ? (
              <button className="seasonal-command danger" disabled={stoppingScan} onClick={stopScan}>{stoppingScan ? '正在停止' : <><Square size={15} />停止扫描</>}</button>
            ) : (
              <button
                className="seasonal-command"
                disabled={busy || scannableBindings.length === 0}
                onClick={scanAllTracking}
                title="扫描当前追更目录并自动更新新增剧集、镜像与元数据"
              ><ScanLine size={16} />扫描更新当前 {scannableBindings.length} 部</button>
            )}
          </div>}
          <div className="category-toolbar-group">
            <SortMenu
              value={sort}
              open={sortOpen}
              onOpenChange={setSortOpen}
              onChange={setSort}
            />
          </div>
          <LibraryViewControls maxColumns={columnCapacity} />
        </div>
      </div>

      {notice && <div className="seasonal-notice" role="status">
        <span>{notice}</span>
        <button type="button" className="seasonal-notice-close" aria-label="关闭扫描结果" title="关闭" onClick={() => setNotice('')}>
          <X size={16} />
        </button>
      </div>}
      {addOpen && <div className="seasonal-dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setAddOpen(false)}>
        <section className="seasonal-dialog" role="dialog" aria-modal="true" aria-label="新增新番">
          <header><div><h2>{addMode === 'root' ? '批量添加新番媒体库' : '添加单部新番'}</h2><p>任务提交后会在后台继续；可以随时关闭并从“查看进度”重新进入。</p></div><button onClick={() => setAddOpen(false)} aria-label="关闭"><X size={18} /></button></header>
          <div className="seasonal-dialog-layout">
            <div className="seasonal-add-form">
              <div className="seasonal-add-mode" role="tablist" aria-label="添加方式">
                <button type="button" disabled={addTaskInFlight} className={addMode === 'root' ? 'active' : ''} onClick={() => setAddMode('root')}>批量根目录</button>
                <button type="button" disabled={addTaskInFlight} className={addMode === 'single' ? 'active' : ''} onClick={() => setAddMode('single')}>单部作品</button>
              </div>
              <div className="seasonal-source-picker">
                <div className="seasonal-source-heading"><strong id="seasonal-source-label">媒体来源</strong><span>选择视频文件实际所在的位置</span></div>
                <div className="seasonal-source-options" role="radiogroup" aria-labelledby="seasonal-source-label">
                  {seasonalSourceOptions.map((option) => {
                    const SourceIcon = option.icon;
                    const selected = draft.source === option.value;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        disabled={busy || addTaskInFlight}
                        className={`seasonal-source-option ${selected ? 'is-selected' : ''}`}
                        onClick={() => setDraft((current) => ({ ...current, source: option.value }))}
                      >
                        <span className="seasonal-source-icon"><SourceIcon size={18} /></span>
                        <span className="seasonal-source-copy"><strong>{option.label}</strong><small>{option.description}</small></span>
                        {selected && <span className="seasonal-source-check" aria-hidden="true"><Check size={13} /></span>}
                      </button>
                    );
                  })}
                </div>
              </div>
              <label><span>{sourcePathCopy.label}</span><div className="seasonal-path-field"><input disabled={addTaskInFlight} value={draft.path} onChange={(event) => setDraft({ ...draft, path: event.target.value })} placeholder={sourcePathCopy.placeholder} /><button type="button" disabled={busy || addTaskInFlight} onClick={() => void chooseTrackingFolder()}><FolderOpen size={16} />选择目录</button></div></label>
              {addMode === 'single' && <>
                <label><span>识别名称（自动，可修改）</span><input disabled={addTaskInFlight} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} placeholder="根据文件夹名称自动识别" /></label>
                <label><span>季度</span><input disabled={addTaskInFlight} type="number" min="0" value={draft.season} onChange={(event) => setDraft({ ...draft, season: event.target.value })} /></label>
              </>}
            </div>

            <aside className={`seasonal-task-panel ${addTaskVisualStatus}`} aria-live="polite">
              <div className="seasonal-task-heading">
                <div><strong>处理进度</strong><span>{addTaskStatusLabel}</span></div>
                <b>{Math.round(addTaskProgress)}%</b>
              </div>
              <div className="seasonal-task-progress" role="progressbar" aria-label="新番导入进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(addTaskProgress)}>
                <span style={{ width: `${addTaskProgress}%` }} />
              </div>
              <div className="seasonal-task-summary">
                <span>{addTaskCurrentTarget}</span>
                {detectedWorkCount > 0 && <strong>已识别 {detectedWorkCount} 部作品</strong>}
              </div>
              <div className="seasonal-task-log" ref={seasonalTaskLogRef} tabIndex={0} aria-label="处理日志">
                {addTaskLogs.length ? addTaskLogs.map((entry, index) => (
                  <div className={entry.kind} key={`${index}-${entry.message}`}><i aria-hidden="true" /><span>{entry.message}</span></div>
                )) : <div className="empty"><span>提交任务后，这里会持续显示扫描、识别、镜像和刮削日志。</span></div>}
              </div>
            </aside>
          </div>
          <footer>
            <span className="seasonal-background-note">{addTaskInFlight ? '关闭弹窗不会停止后台任务' : latestAddTask ? '可查看上一次任务的完整结果' : '准备好后提交任务'}</span>
            <div>
              <button onClick={() => setAddOpen(false)}>{addTaskInFlight ? '关闭到后台' : '关闭'}</button>
              <button className="primary" disabled={busy || addTaskInFlight || !draft.path.trim()} onClick={addTracking}>{busy ? '正在提交...' : addTaskInFlight ? '后台处理中' : addMode === 'root' ? '识别全部作品并加入' : '扫描并加入'}</button>
            </div>
          </footer>
        </section>
      </div>}

      {categoryWorks.length === 0 ? (
        <CenteredMessage>这个筛选下还没有作品</CenteredMessage>
      ) : (
        <div className="category-grid-wrap">
          <VirtualizedPosterGrid
            works={categoryWorks}
            columns={columnsPerRow}
            onColumnCapacityChange={setColumnCapacity}
            localArtworkOnly
          />
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
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const current = getSortOption(value);

  useDismissiblePopover(open, () => {
    onOpenChange(false);
    triggerRef.current?.focus();
  }, menuRef);

  const selectSortDimension = (dimension: typeof sortDimensions[number]) => {
    onChange(toggleSort(value, dimension));
  };

  return (
    <div className="sort-menu-wrap" ref={menuRef}>
      <button
        ref={triggerRef}
        className={`sort-trigger ${open ? 'active' : ''}`}
        onClick={() => onOpenChange(!open)}
        title="排序"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span>{current.directionLabel ? `${current.label} ${current.directionLabel}` : current.label}</span>
        <ChevronDown size={15} strokeWidth={1.8} />
      </button>
      {open && (
        <div className="sort-menu" role="menu" aria-label="排序方式">
          {sortDimensions.map((dimension) => {
            const option = getSortOption(dimension === 'recent' ? 'recent' : toggleSort('recent', dimension));
            const active = getSortDimension(value) === dimension;
            return (
              <button
                key={dimension}
                className={active ? 'active' : ''}
                role="menuitemradio"
                aria-checked={active}
                onClick={() => selectSortDimension(dimension)}
              >
                <span>{option.label}</span>
                {active && current.directionLabel && <span>{current.directionLabel}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function CenteredMessage({ children }: { children: string }) {
  return (
    <div className="page-loading-wrap">
      <div className="page-loading-message">{children}</div>
    </div>
  );
}
