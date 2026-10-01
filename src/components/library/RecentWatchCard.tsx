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
  /** 第二层左半：`S01E01` 或 `电影`。 */
  episodeLabel: string;
  /** 第二层右半：`还剩 23 分钟` / `已看完 ✓` / `继续观看`。 */
  statusLabel: string;
  /** 第三层：`今天 12:50` 这类弱化时间。 */
  timeLabel: string;
}

/**
 * 「最近观看」卡片：继续播放入口，不是播放历史日志。
 *
 * 视觉层级固定为三层（作品名 / 集号·剩余时间 / 时间），图片本身承担第一视觉焦点，
 * 播放进度只由图片底部的 3px 进度条表达 —— 精确位置、总时长与百分比**不同时**以文本
 * 出现（依据 Plex / Infuse 类 Continue Watching 的成熟做法与 Windows 进度控件规范：
 * 时长可预期的确定型进度条是非交互、只读的反馈，不应再重复一份文字）。
 */
export default function RecentWatchCard({ work, history, episodeLabel, statusLabel, timeLabel }: RecentWatchCardProps) {
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
  const status = [episodeLabel, statusLabel].filter(Boolean).join(' · ');

  return (
    <button
      type="button"
      className="recent-watch-card"
      aria-label={[title, status, timeLabel].filter(Boolean).join(' · ')}
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
          {episodeLabel && <span className="recent-watch-code">{episodeLabel}</span>}
          {episodeLabel && statusLabel && <span className="recent-watch-separator" aria-hidden="true">·</span>}
          {statusLabel && <span className="recent-watch-status">{statusLabel}</span>}
        </span>
        {timeLabel && <time className="recent-watch-time" dateTime={history.played_at || history.updated_at}>{timeLabel}</time>}
      </span>
    </button>
  );
}
