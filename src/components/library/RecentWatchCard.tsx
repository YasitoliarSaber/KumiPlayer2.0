import { useState } from 'react';
import type { PlaybackHistoryItem, WorkIndex } from '../../api/types';
import { buildAssetUrl } from '../../api/assets';
import { useLibraryStore } from '../../stores/library';
import { cleanDisplayTitle } from '../../utils/title';
import { preferredArtworkPath } from '../../utils/artwork';
import DecodedImage, { type ImageState } from '../ui/DecodedImage';

interface RecentWatchCardProps {
  work: WorkIndex;
  history: PlaybackHistoryItem;
  episodeLabel: string;
  progressLabel: string;
  timeLabel: string;
}

export default function RecentWatchCard({ work, history, episodeLabel, progressLabel, timeLabel }: RecentWatchCardProps) {
  const openWorkDetail = useLibraryStore((state) => state.openWorkDetail);
  const [imageState, setImageState] = useState<ImageState>('loading');
  const title = cleanDisplayTitle(work.title || work.original_title || history.title_snapshot || '') || '未命名作品';
  const movie = work.media_type === 'movie';
  const path = history.thumb_path || (movie ? preferredArtworkPath(work, 'fanart') : '');
  const imageUrl = buildAssetUrl(path, { kind: 'episode', thumbnailWidth: 640 });
  const position = Number(history.position);
  const duration = Number(history.duration);
  const percent = history.completed ? 100
    : Number.isFinite(position) && Number.isFinite(duration) && duration > 0
      ? Math.min(100, Math.max(0, (position / duration) * 100)) : 0;
  const placeholder = !imageUrl ? movie ? '暂无图片' : '暂无分集图片'
    : imageState === 'error' ? '图片暂不可用' : '图片加载中';

  return (
    <button
      type="button"
      className="recent-watch-card"
      aria-label={[title, episodeLabel, progressLabel].filter(Boolean).join(' · ')}
      onClick={() => void openWorkDetail(work.work_id)}
    >
      <span className="recent-watch-media">
        <span className="recent-watch-placeholder" aria-hidden="true">{placeholder}</span>
        {imageUrl && <DecodedImage
          src={imageUrl}
          alt={`${title} · ${episodeLabel}`}
          className="recent-watch-image"
          loading="lazy"
          onStateChange={setImageState}
        />}
        {percent > 0 && <span className="recent-watch-progress" aria-hidden="true">
          <span style={{ width: `${percent}%` }} />
        </span>}
      </span>
      <span className="recent-watch-copy">
        <span className="recent-watch-title" title={title}>{title}</span>
        <span className="recent-watch-position">
          <span className="recent-watch-code">{episodeLabel}</span>
          {progressLabel && <span className="recent-watch-duration">{progressLabel}</span>}
        </span>
        {timeLabel && <time className="recent-watch-time" dateTime={history.played_at || history.updated_at}>{timeLabel}</time>}
      </span>
    </button>
  );
}
