import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import PosterCard from './PosterCard';
import { useUiStore } from '../../stores/ui';
import { calculatePosterGridMetrics, hasVisibleWindowChanged } from './posterGridMetrics';

interface VirtualizedPosterGridProps {
  works: any[];
  columns: number;
  recentLabel?: string;
  onColumnCapacityChange?: (capacity: number) => void;
  localArtworkOnly?: boolean;
}

const OVERSCAN_ROWS = 1;

// 缩略图档选择：按卡片 CSS 宽度 × DPR 向上取最近档，不超过 512。
// 384 覆盖单列 140-256px @ DPR2，512 覆盖更大列宽或高 DPR。
function pickThumbnailWidth(cssWidth: number): number {
  const dpr = typeof window !== 'undefined' ? Math.min(window.devicePixelRatio || 1, 2) : 1
  const deviceWidth = Math.ceil(cssWidth * dpr)
  if (deviceWidth <= 384) return 384
  return 512
}

export default function VirtualizedPosterGrid({
  works,
  columns,
  recentLabel = '',
  onColumnCapacityChange,
  localArtworkOnly = false,
}: VirtualizedPosterGridProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const { seriesCardImageMode } = useUiStore();
  const requestedColumns = Math.max(1, Math.round(columns || 1));
  const [effectiveColumns, setEffectiveColumns] = useState(requestedColumns);
  const [layout, setLayout] = useState({
    top: 0,
    rowHeight: 320,
    columnWidth: 0,
    viewportHeight: typeof window === 'undefined' ? 900 : window.innerHeight,
    scrollTop: typeof window === 'undefined' ? 0 : window.scrollY,
  });

  useLayoutEffect(() => {
    let measureFrame = 0;
    let scrollFrame = 0;
    const scrollContainer = wrapRef.current?.closest<HTMLElement>('.app-main') || null;
    const currentScrollTop = () => scrollContainer ? scrollContainer.scrollTop : window.scrollY;
    const currentViewportHeight = () => scrollContainer ? scrollContainer.clientHeight : window.innerHeight;

    const measure = () => {
      measureFrame = 0;
      const element = wrapRef.current;
      if (!element) return;
      const rect = element.getBoundingClientRect();
      const containerRect = scrollContainer?.getBoundingClientRect();
      const width = element.clientWidth || rect.width || 1;
      const gap = readGridGap(element);
      const metaHeight = readCssLength(element, '--poster-card-meta-height', 62);
      const metrics = calculatePosterGridMetrics({
        width,
        gap,
        requestedColumns,
        imageMode: seriesCardImageMode,
        metaHeight,
      });
      const measuredColumns = metrics.effectiveColumns;
      onColumnCapacityChange?.(metrics.columnCapacity);
      setEffectiveColumns((current) => current === measuredColumns ? current : measuredColumns);
      setLayout({
        top: scrollContainer && containerRect
          ? rect.top - containerRect.top + currentScrollTop()
          : rect.top + window.scrollY,
        rowHeight: metrics.rowHeight,
        columnWidth: metrics.columnWidth,
        viewportHeight: currentViewportHeight(),
        scrollTop: currentScrollTop(),
      });
    };

    const requestMeasure = () => {
      if (measureFrame) return;
      measureFrame = window.requestAnimationFrame(measure);
    };

    const updateScrollPosition = () => {
      if (scrollFrame) return;
      scrollFrame = window.requestAnimationFrame(() => {
        scrollFrame = 0;
        const scrollTop = currentScrollTop();
        setLayout((current) => {
          if (!hasVisibleWindowChanged(current, scrollTop)) return current;
          return { ...current, scrollTop };
        });
      });
    };

    measure();
    if (scrollContainer) {
      scrollContainer.addEventListener('scroll', updateScrollPosition, { passive: true });
    } else {
      window.addEventListener('scroll', updateScrollPosition, { passive: true });
    }
    window.addEventListener('resize', requestMeasure);
    window.visualViewport?.addEventListener('resize', requestMeasure);

    let resolutionQuery = window.matchMedia?.(`(resolution: ${window.devicePixelRatio}dppx)`);
    const handleResolutionChange = () => {
      requestMeasure();
      resolutionQuery?.removeEventListener('change', handleResolutionChange);
      resolutionQuery = window.matchMedia?.(`(resolution: ${window.devicePixelRatio}dppx)`);
      resolutionQuery?.addEventListener('change', handleResolutionChange);
    };
    resolutionQuery?.addEventListener('change', handleResolutionChange);

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined' && wrapRef.current) {
      observer = new ResizeObserver(requestMeasure);
      observer.observe(wrapRef.current);
    }

    return () => {
      if (measureFrame) window.cancelAnimationFrame(measureFrame);
      if (scrollFrame) window.cancelAnimationFrame(scrollFrame);
      observer?.disconnect();
      if (scrollContainer) {
        scrollContainer.removeEventListener('scroll', updateScrollPosition);
      } else {
        window.removeEventListener('scroll', updateScrollPosition);
      }
      window.removeEventListener('resize', requestMeasure);
      window.visualViewport?.removeEventListener('resize', requestMeasure);
      resolutionQuery?.removeEventListener('change', handleResolutionChange);
    };
  }, [onColumnCapacityChange, requestedColumns, seriesCardImageMode, works.length]);

  const totalRows = Math.ceil(works.length / effectiveColumns);
  const visible = useMemo(() => {
    const relativeTop = Math.max(0, layout.scrollTop - layout.top);
    const rawStartRow = Math.max(0, Math.floor(relativeTop / layout.rowHeight) - OVERSCAN_ROWS);
    const maxStartRow = Math.max(0, totalRows - 1);
    const startRow = Math.min(maxStartRow, rawStartRow);
    const rawEndRow = Math.ceil((relativeTop + layout.viewportHeight) / layout.rowHeight) + OVERSCAN_ROWS;
    const endRow = totalRows === 0 ? -1 : Math.max(
      startRow,
      Math.min(totalRows - 1, rawEndRow),
    );
    const startIndex = startRow * effectiveColumns;
    const endIndex = Math.min(works.length, (endRow + 1) * effectiveColumns);
    return {
      startRow,
      endRow,
      items: works.slice(startIndex, endIndex),
    };
  }, [effectiveColumns, layout, totalRows, works]);

  const topSpacer = visible.startRow * layout.rowHeight;
  const renderedRows = Math.max(0, visible.endRow - visible.startRow + 1);
  const bottomSpacer = Math.max(0, (totalRows - visible.startRow - renderedRows) * layout.rowHeight);
  // 分类页卡片走派生缩略图，降低本地 w780 原图的解码与内存开销
  const thumbnailWidth = layout.columnWidth > 0 ? pickThumbnailWidth(layout.columnWidth) : 0;

  return (
    <div
      ref={wrapRef}
      className="virtual-poster-grid"
      style={{
        ['--category-columns' as string]: effectiveColumns,
      }}
    >
      {topSpacer > 0 && <div style={{ height: topSpacer }} aria-hidden="true" />}
      <div className="category-grid">
        {visible.items.map((work) => (
          <PosterCard
            key={`${work.source}:${work.work_id}`}
            work={work}
            recentLabel={recentLabel}
            thumbnailWidth={thumbnailWidth}
            localArtworkOnly={localArtworkOnly}
          />
        ))}
      </div>
      {bottomSpacer > 0 && <div style={{ height: bottomSpacer }} aria-hidden="true" />}
    </div>
  );
}

function readGridGap(element: HTMLElement) {
  const computed = window.getComputedStyle(element);
  const value = Number.parseFloat(computed.getPropertyValue('--virtual-grid-gap'));
  if (Number.isFinite(value) && value > 0) return value;
  return Math.max(22, Math.min(32, window.innerWidth * 0.0155));
}

function readCssLength(element: HTMLElement, property: string, fallback: number) {
  const value = Number.parseFloat(window.getComputedStyle(element).getPropertyValue(property));
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}
