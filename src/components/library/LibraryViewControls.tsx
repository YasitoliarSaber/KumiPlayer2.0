import { useEffect, useState, type CSSProperties, type FormEvent } from 'react';
import { RectangleHorizontal, RectangleVertical } from 'lucide-react';
import { configApi } from '../../api/config';
import { useUiStore, type SeriesCardImageMode } from '../../stores/ui';

const MIN_COLUMNS_PER_ROW = 4;
const MAX_COLUMNS_PER_ROW = 8;

interface LibraryViewControlsProps {
  maxColumns?: number;
}

export default function LibraryViewControls({ maxColumns }: LibraryViewControlsProps) {
  const {
    posterSize,
    setPosterSize,
    seriesCardImageMode,
    setSeriesCardImageMode,
  } = useUiStore();
  const columnsPerRow = normalizeColumns(posterSize);
  const effectiveMaxColumns = normalizeColumnCapacity(maxColumns);
  const effectiveMinColumns = Math.min(MIN_COLUMNS_PER_ROW, effectiveMaxColumns);
  const columnControlLocked = effectiveMaxColumns <= MIN_COLUMNS_PER_ROW;
  const [draftColumns, setDraftColumns] = useState(columnsPerRow);
  const visibleColumns = Math.min(draftColumns, effectiveMaxColumns);
  const columnRange = effectiveMaxColumns - effectiveMinColumns;
  const columnRangeProgress = columnRange > 0
    ? ((visibleColumns - effectiveMinColumns) / columnRange) * 100
    : 0;

  useEffect(() => {
    setDraftColumns(columnsPerRow);
  }, [columnsPerRow]);

  const applyCardMode = (mode: SeriesCardImageMode) => {
    setSeriesCardImageMode(mode);
    configApi.patchConfig({ series_card_image_mode: mode }).catch((error) => {
      console.error('Failed to persist card mode:', error);
    });
  };

  const applyPosterSize = (columns: number) => {
    const normalized = normalizeColumns(columns);
    if (normalized === columnsPerRow) return;
    setPosterSize(normalized);
    configApi.patchConfig({ poster_size: normalized }).catch((error) => {
      console.error('Failed to persist poster size:', error);
    });
  };

  const handleColumnsInput = (event: FormEvent<HTMLInputElement>) => {
    if (columnControlLocked) return;
    const nextColumns = Math.round(Number(event.currentTarget.value));
    setDraftColumns(Math.max(effectiveMinColumns, Math.min(effectiveMaxColumns, nextColumns)));
  };

  const commitDraftColumns = () => {
    if (columnControlLocked) return;
    applyPosterSize(draftColumns);
  };

  return (
    <div className="category-toolbar-group library-view-controls" role="group" aria-label="作品卡片布局">
        <div className="category-segment" title="卡片图片比例" aria-label="卡片图片比例">
          <ModeButton
            label="竖版封面"
            active={seriesCardImageMode === 'poster'}
            onClick={() => applyCardMode('poster')}
            mode="poster"
          />
          <ModeButton
            label="横向海报"
            active={seriesCardImageMode === 'fanart'}
            onClick={() => applyCardMode('fanart')}
            mode="fanart"
          />
        </div>

        <label
          className="card-size-control"
          title={`调整每行封面数量，当前窗口最多 ${effectiveMaxColumns} 个`}
        >
          <input
            className="modern-range library-card-size-slider"
            type="range"
            min={effectiveMinColumns}
            max={effectiveMaxColumns}
            step="1"
            value={visibleColumns}
            aria-label="每行显示作品数量"
            aria-valuetext={`每行 ${visibleColumns} 个作品`}
            style={{ '--range-progress': `${columnRangeProgress}%` } as CSSProperties}
            disabled={columnControlLocked}
            onInput={handleColumnsInput}
            onChange={handleColumnsInput}
            onPointerUp={commitDraftColumns}
            onKeyUp={commitDraftColumns}
            onBlur={commitDraftColumns}
          />
        </label>
    </div>
  );
}

export function normalizeColumns(value: number) {
  if (Number.isFinite(value) && value > 0 && value <= MAX_COLUMNS_PER_ROW) {
    return Math.max(MIN_COLUMNS_PER_ROW, Math.min(MAX_COLUMNS_PER_ROW, Math.round(value)));
  }
  if (value >= 210) return 4;
  if (value >= 170) return 5;
  return 6;
}

function normalizeColumnCapacity(value: number | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return MAX_COLUMNS_PER_ROW;
  return Math.max(1, Math.min(MAX_COLUMNS_PER_ROW, Math.round(value || 1)));
}

function ModeButton({
  active,
  label,
  mode,
  onClick,
}: {
  active: boolean;
  label: string;
  mode: SeriesCardImageMode;
  onClick: () => void;
}) {
  const ModeIcon = mode === 'poster' ? RectangleVertical : RectangleHorizontal;

  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={active}
      className={`category-mode-btn ${active ? 'active' : ''}`}
    >
      <ModeIcon size={17} strokeWidth={1.85} />
    </button>
  );
}
