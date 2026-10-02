export type PosterGridImageMode = 'poster' | 'fanart';

interface PosterGridMetricsInput {
  width: number;
  gap: number;
  requestedColumns: number;
  imageMode: PosterGridImageMode;
  metaHeight: number;
}

export interface PosterGridMetrics {
  columnCapacity: number;
  effectiveColumns: number;
  columnWidth: number;
  rowHeight: number;
}

const MIN_CARD_WIDTH: Record<PosterGridImageMode, number> = {
  poster: 140,
  fanart: 190,
};

export function calculatePosterGridMetrics({
  width,
  gap,
  requestedColumns,
  imageMode,
  metaHeight,
}: PosterGridMetricsInput): PosterGridMetrics {
  const safeWidth = Math.max(1, Number.isFinite(width) ? width : 1);
  const safeGap = Math.max(0, Number.isFinite(gap) ? gap : 0);
  const safeMetaHeight = Math.max(0, Number.isFinite(metaHeight) ? metaHeight : 0);
  const safeRequestedColumns = Math.max(1, Math.round(requestedColumns || 1));
  const minimumCardWidth = MIN_CARD_WIDTH[imageMode];
  const availableColumns = Math.max(
    1,
    Math.floor((safeWidth + safeGap) / (minimumCardWidth + safeGap)),
  );
  const effectiveColumns = Math.min(safeRequestedColumns, availableColumns);
  const columnWidth = Math.max(
    1,
    (safeWidth - safeGap * (effectiveColumns - 1)) / effectiveColumns,
  );
  const mediaHeight = imageMode === 'fanart'
    ? columnWidth * 9 / 16
    : columnWidth * 3 / 2;

  return {
    columnCapacity: availableColumns,
    effectiveColumns,
    columnWidth,
    rowHeight: mediaHeight + safeMetaHeight + safeGap,
  };
}
