import { memo, useEffect, useRef, useState } from 'react';
import { useUiStore } from '../../stores/ui';
import { useLibraryStore } from '../../stores/library';
import { cleanDisplayTitle } from '../../utils/title';
import { buildAssetUrl, isRemoteAssetPath } from '../../api/assets';
import { isScrollRecentlyActive } from '../../utils/scrollGesture';
import { preferredArtworkPath } from '../../utils/artwork';
import DecodedImage, { type ImageState } from '../ui/DecodedImage';
import PosterImage from './PosterImage';
import './PosterCard.css';

interface PosterCardProps {
  work: any;
  showType?: 'default' | 'recent';
  recentLabel?: string;
  thumbnailWidth?: number;
  localArtworkOnly?: boolean;
  deferImage?: boolean;
  preloadImage?: boolean;
  managedImage?: boolean;
}

function PosterCard({
  work,
  showType = 'default',
  recentLabel = '',
  thumbnailWidth = 0,
  localArtworkOnly = false,
  deferImage = false,
  preloadImage = false,
  managedImage = false,
}: PosterCardProps) {
  const [imageState, setImageState] = useState<ImageState>('loading');
  const seriesCardImageMode = useUiStore((state) => state.seriesCardImageMode);
  const openWorkDetail = useLibraryStore((state) => state.openWorkDetail);
  const getWorkDetail = useLibraryStore((state) => state.getWorkDetail);
  const prewarmTimerRef = useRef<number | null>(null);
  const rawTitle = work.title || work.original_title || work.local_title || '';
  const displayTitle = cleanDisplayTitle(rawTitle) || '未命名作品';
  // P-001 阶段6：季度数由后端权威投影提供，不再用空数组 || 1 伪造一季。
  const mainSeasonCount = Number(work.season_count || 0);
  const seasonLabel = mainSeasonCount > 0
    ? `共 ${mainSeasonCount} 季`
    : work.show_type === 'anime_series' || work.show_type === 'live_series'
      ? '季度待确认'
      : '';

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
  const artworkKind = showType === 'recent' || seriesCardImageMode === 'fanart' ? 'fanart' : 'poster';
  // 分类本地模式不隐式访问远程；其他页面仍可使用受限远程图片代理。
  const preferredImagePath = preferredArtworkPath(work, artworkKind);
  const selectedImagePath = localArtworkOnly && isRemoteAssetPath(preferredImagePath) ? '' : preferredImagePath;
  const originalImageUrl = buildAssetUrl(selectedImagePath, {
    kind: isHorizontal ? 'backdrop' : 'poster',
  });
  // 远程图已按尺寸档归一化，不生成本地派生缩略图 URL；本地图继续走缩略图管线。
  // 第 6 步（规格 §4.5）：横图**也要**用按尺寸生成的缩略图；此前 `!isHorizontal`
  // 让横图直接取原图，等于把大背景图塞进小卡。
  const thumbnailImageUrl = isRemoteAssetPath(selectedImagePath)
    ? originalImageUrl
    : buildAssetUrl(selectedImagePath, {
        kind: isHorizontal ? 'backdrop' : 'poster',
        ...(thumbnailWidth > 0 ? { thumbnailWidth } : {}),
        cacheOnly: managedImage,
      });
  const imageUrl = thumbnailImageUrl;

  const mediaClassName = `poster-media ${isHorizontal ? 'poster-media-horizontal' : 'poster-media-vertical'}`;

  return (
    <button
      onClick={handleClick}
      onPointerEnter={schedulePrewarm}
      onPointerLeave={cancelPrewarm}
      onFocus={() => {
        // 滚动过程中聚焦变化会触发 getWorkDetail，造成抖动；仅在非滚动时触发
        if (!isScrollRecentlyActive()) void getWorkDetail(work.work_id).catch(() => undefined);
      }}
      className="poster-card text-left focus:outline-none"
    >
      <div
        className={mediaClassName}
        style={{ background: 'var(--surface-soft)' }}
      >
        {/* 标题常驻底层，加载失败时也可辨认作品。分类图片不回退到大原图。 */}
        <div className="poster-placeholder-title">
          <span title={displayTitle}>
            {displayTitle}
          </span>
          {!managedImage && !deferImage && imageUrl && imageState !== 'ready' && (
            <small className="poster-placeholder-state">
              {imageState === 'loading' ? '图片加载中' : '图片暂不可用'}
            </small>
          )}
        </div>
        {!deferImage && imageUrl && (managedImage ? (
          <PosterImage path={selectedImagePath} width={thumbnailWidth} src={imageUrl}
            alt={displayTitle} local={!isRemoteAssetPath(selectedImagePath)} />
        ) : (
          <DecodedImage
            src={imageUrl}
            alt={displayTitle}
            loading={preloadImage ? 'eager' : 'lazy'}
            fetchPriority={preloadImage ? 'auto' : undefined}
            onStateChange={setImageState}
            className="poster-image"
          />
        ))}

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
          {recentLabel
            ? recentLabel
            : seasonLabel
              ? seasonLabel
              : work.year ? `${work.year}` : ''
          }
        </div>
        {(work.watch_status?.status === 'watching' || work.watch_status?.status === 'on_hold') && (
          <div className="poster-card-seasonal">
            <span className="seasonal-dot" aria-hidden="true" />
            {work.watch_status.status === 'watching' ? '追更中' : '搁置中'}
            {work.latest_episode_number != null && ` · 更新至 ${work.latest_episode_number} 集`}
          </div>
        )}
      </div>
    </button>
  );
}

export default memo(PosterCard);
