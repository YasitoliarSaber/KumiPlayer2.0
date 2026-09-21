import { memo, useEffect, useRef, useState } from 'react';
import { useUiStore } from '../../stores/ui';
import { useLibraryStore } from '../../stores/library';
import { cleanDisplayTitle } from '../../utils/title';
import { buildAssetUrl, isRemoteAssetPath } from '../../api/assets';
import { isScrollRecentlyActive } from '../../utils/scrollGesture';
import { preferredArtworkPath } from '../../utils/artwork';
import DecodedImage from '../ui/DecodedImage';

interface PosterCardProps {
  work: any;
  showType?: 'default' | 'recent';
  recentLabel?: string;
  thumbnailWidth?: number;
  localArtworkOnly?: boolean;
}

function PosterCard({
  work,
  showType = 'default',
  recentLabel = '',
  thumbnailWidth = 0,
  localArtworkOnly = false,
}: PosterCardProps) {
  const seriesCardImageMode = useUiStore((state) => state.seriesCardImageMode);
  // 该 prop 仍是调用方的公开 API（分类页会传），但第 6 步移除"失败回退原图"后
  // 组件内不再需要它；显式标记为有意保留，避免 noUnusedLocals 报错。
  void localArtworkOnly;
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
  // 已确认的本地镜像比远程 metadata URL 更快、更稳定；远程图仍是本地缺失时的兜底。
  const selectedImagePath = preferredArtworkPath(work, artworkKind);
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
        {/* 第 6 步（规格 §4.5）：标题占位**常驻底层**，图片解码成功后自然覆盖它。
            这样"有 URL 但未解码/加载失败"时不会再出现空白海报位（白卡）；
            同时**不再**在缩略图失败后重复请求原图（后端生成失败时已返回原图）。 */}
        <div className="poster-placeholder-title">
          <span title={displayTitle}>
            {displayTitle}
          </span>
        </div>
        {imageUrl && (
          <DecodedImage
            src={imageUrl}
            alt={displayTitle}
            loading="lazy"
            className="poster-image"
          />
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
