import { useEffect, useRef, useState } from 'react';
import { useUiStore } from '../../stores/ui';
import { useLibraryStore } from '../../stores/library';
import { cleanDisplayTitle } from '../../utils/title';
import { buildAssetUrl, isRemoteAssetPath } from '../../api/assets';
import { isScrollRecentlyActive } from '../../utils/scrollGesture';
import DecodedImage from '../ui/DecodedImage';

interface PosterCardProps {
  work: any;
  showType?: 'default' | 'recent';
  recentLabel?: string;
  thumbnailWidth?: number;
  localArtworkOnly?: boolean;
}

export default function PosterCard({
  work,
  showType = 'default',
  recentLabel = '',
  thumbnailWidth = 0,
  localArtworkOnly = false,
}: PosterCardProps) {
  const { seriesCardImageMode } = useUiStore();
  const openWorkDetail = useLibraryStore((state) => state.openWorkDetail);
  const getWorkDetail = useLibraryStore((state) => state.getWorkDetail);
  const prewarmTimerRef = useRef<number | null>(null);
  const rawTitle = work.title || work.original_title || work.local_title || '';
  const displayTitle = cleanDisplayTitle(rawTitle) || '未命名作品';
  const mainSeasonCount = (work.seasons || []).filter((season: any) => (season.group_type || 'season') === 'season').length || 1;

  const handleClick = () => {
    void openWorkDetail(work.work_id);
  };

  const schedulePrewarm = () => {
    if (isScrollRecentlyActive()) return;
    if (prewarmTimerRef.current !== null) window.clearTimeout(prewarmTimerRef.current);
    prewarmTimerRef.current = window.setTimeout(() => {
      prewarmTimerRef.current = null;
      if (isScrollRecentlyActive()) return;
      void getWorkDetail(work.work_id).catch(() => undefined);
    }, 180);
  };

  const cancelPrewarm = () => {
    if (prewarmTimerRef.current !== null) window.clearTimeout(prewarmTimerRef.current);
    prewarmTimerRef.current = null;
  };

  useEffect(() => {
    isScrollRecentlyActive();
    return cancelPrewarm;
  }, []);

  const isHorizontal = showType === 'recent' || seriesCardImageMode === 'fanart';
  const imagePath = showType === 'recent'
    ? work.fanart_path
    : (seriesCardImageMode === 'fanart' ? work.fanart_path : work.poster_path);
  const localImagePath = showType === 'recent'
    ? work.local_fanart_path
    : (seriesCardImageMode === 'fanart' ? work.local_fanart_path : work.local_poster_path);
  // 本地优先、远程兜底：localArtworkOnly 只决定优先顺序，本地缺失时仍回退 canonical 远程图。
  const preferredImagePath = localImagePath || imagePath;
  const selectedImagePath = localArtworkOnly ? preferredImagePath : imagePath;
  const [useOriginalImage, setUseOriginalImage] = useState(false);
  const originalImageUrl = buildAssetUrl(selectedImagePath, {
    kind: isHorizontal ? 'backdrop' : 'poster',
  });
  // 远程图已按尺寸档归一化，不生成本地派生缩略图 URL；本地图继续走缩略图管线。
  const thumbnailImageUrl = isRemoteAssetPath(selectedImagePath)
    ? originalImageUrl
    : buildAssetUrl(selectedImagePath, {
        kind: isHorizontal ? 'backdrop' : 'poster',
        ...(thumbnailWidth > 0 && !isHorizontal ? { thumbnailWidth } : {}),
      });
  const imageUrl = useOriginalImage ? originalImageUrl : thumbnailImageUrl;

  useEffect(() => {
    setUseOriginalImage(false);
  }, [selectedImagePath, thumbnailWidth, isHorizontal, localArtworkOnly]);

  const isSeasonalImport = work.import_scope === 'seasonal';
  const mediaClassName = `poster-media ${isHorizontal ? 'poster-media-horizontal' : 'poster-media-vertical'}`;

  return (
    <button
      onClick={handleClick}
      onPointerEnter={schedulePrewarm}
      onPointerLeave={cancelPrewarm}
      onFocus={() => void getWorkDetail(work.work_id).catch(() => undefined)}
      className="poster-card text-left focus:outline-none"
    >
      <div
        className={mediaClassName}
        style={{ background: 'var(--surface-soft)' }}
      >
        {imageUrl ? (
          <DecodedImage
            src={imageUrl}
            alt={displayTitle}
            loading="lazy"
            className="poster-image"
            onError={() => {
              if (thumbnailImageUrl !== originalImageUrl) setUseOriginalImage(true);
            }}
          />
        ) : (
          <div className="poster-placeholder-title">
            <span title={displayTitle}>
              {displayTitle}
            </span>
          </div>
        )}

        {work.rating > 0 && (
          <div className="rating-badge">
            ★ {work.rating.toFixed(1)}
          </div>
        )}
      </div>

      <div className="poster-card-meta">
        <div className="poster-card-title" style={{ color: 'var(--text)' }} title={rawTitle || displayTitle}>
          {displayTitle}
        </div>
        <div className="poster-card-subtitle" style={{ color: 'var(--text-muted)' }}>
          {isSeasonalImport
            ? `追更中 · ${latestEpisodeLabel(work)}`
            : recentLabel
            ? recentLabel
            : work.show_type === 'anime_series' || work.show_type === 'live_series' 
            ? `共 ${mainSeasonCount} 季`
            : work.year ? `${work.year}` : ''
          }
        </div>
      </div>
    </button>
  );
}

function latestEpisodeLabel(work: any) {
  const summarized = Number(work.latest_episode_number || 0);
  if (summarized > 0) return `更新至 ${summarized} 集`;
  const numbers = (work.episodes || [])
    .filter((episode: any) => episode.group_type === 'season')
    .map((episode: any) => Number(episode.episode_number || 0));
  const latest = numbers.length ? Math.max(...numbers) : 0;
  return latest > 0 ? `更新至 ${latest} 集` : '等待剧集';
}
