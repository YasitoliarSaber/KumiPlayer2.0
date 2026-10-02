import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import PosterCard from './PosterCard';
import { useUiStore } from '../../stores/ui';
import { calculatePosterGridMetrics } from './posterGridMetrics';
import { PosterImageContext, PosterImageLifecycle } from './posterImageLifecycle';

interface VirtualizedPosterGridProps {
  works: any[];
  columns: number;
  recentLabel?: string;
  onColumnCapacityChange?: (capacity: number) => void;
  localArtworkOnly?: boolean;
}

// 卡片宽度与屏幕缩放共同决定固定档位。
function pickThumbnailWidth(cssWidth: number, horizontal: boolean): number {
  const dpr = typeof window !== 'undefined' ? Math.min(window.devicePixelRatio || 1, 2) : 1
  const deviceWidth = Math.ceil(cssWidth * dpr)
  if (horizontal) return deviceWidth <= 512 ? 512 : 768
  if (deviceWidth <= 256) return 256
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
  const imageLifecycle = useMemo(() => new PosterImageLifecycle(), []);
  // P0-5：字段选择器订阅（避免 UI store 其他字段变化触发网格重渲染）
  const seriesCardImageMode = useUiStore((state) => state.seriesCardImageMode);
  const requestedColumns = Math.max(1, Math.round(columns || 1));
  const [effectiveColumns, setEffectiveColumns] = useState(requestedColumns);
  const [columnWidth, setColumnWidth] = useState(0);

  useLayoutEffect(() => {
    let measureFrame = 0;
    const scrollContainer = wrapRef.current?.closest<HTMLElement>('.app-main') || null;
    const currentViewportHeight = () => scrollContainer ? scrollContainer.clientHeight : window.innerHeight;

    const measure = () => {
      measureFrame = 0;
      const element = wrapRef.current;
      if (!element) return;
      const rect = element.getBoundingClientRect();
      const width = element.clientWidth || rect.width;
      // 隐藏窗口尚无有效尺寸时保留卡片壳，不能按假宽度请求图片。
      if (width <= 0) return;
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
      setColumnWidth(metrics.columnWidth);
      imageLifecycle.configure(scrollContainer, currentViewportHeight());
    };

    const requestMeasure = () => {
      if (measureFrame) return;
      measureFrame = window.requestAnimationFrame(measure);
    };

    measure();
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
      observer?.disconnect();
      imageLifecycle.dispose();
      window.removeEventListener('resize', requestMeasure);
      window.visualViewport?.removeEventListener('resize', requestMeasure);
      resolutionQuery?.removeEventListener('change', handleResolutionChange);
    };
  }, [imageLifecycle, onColumnCapacityChange, requestedColumns, seriesCardImageMode, works.length]);
  // 分类页卡片走派生缩略图，降低本地 w780 原图的解码与内存开销
  const thumbnailWidth = columnWidth > 0 ? pickThumbnailWidth(columnWidth, seriesCardImageMode === 'fanart') : 0;

  return (
    <PosterImageContext.Provider value={imageLifecycle}>
    <div
      ref={wrapRef}
      className="virtual-poster-grid"
      style={{
        ['--category-columns' as string]: effectiveColumns,
      }}
    >
      <div className="category-grid">
        {works.map((work) => (
          <PosterCard
            key={`${work.source}:${work.work_id}`}
            work={work}
            recentLabel={recentLabel}
            thumbnailWidth={thumbnailWidth}
            deferImage={thumbnailWidth === 0}
            managedImage
            localArtworkOnly={localArtworkOnly}
          />
        ))}
      </div>
    </div>
    </PosterImageContext.Provider>
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
