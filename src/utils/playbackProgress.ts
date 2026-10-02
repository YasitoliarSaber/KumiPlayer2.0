import type { PlaybackHistoryItem, PlaybackSession } from '../api/types';

export type PlaybackProgressItem = PlaybackHistoryItem & {
  ratio?: number;
  bangumi_synced?: boolean;
  bangumi_error?: string;
  manually_unwatched?: boolean;
};

export type PlaybackStatusSnapshot = {
  status: string;
  session: (PlaybackSession & {
    position?: number;
    duration?: number;
    started_at?: string;
  }) | null;
};

export function resolvePlaybackActionLabel(
  playbackStatus: PlaybackStatusSnapshot | null,
  workId: string,
  episodeId: string,
  progress?: PlaybackProgressItem | null,
): '正在播放' | '继续播放' | '开始播放' {
  const session = playbackStatus?.session;
  if (playbackStatus?.status === 'playing' && session?.work_id === workId
    && session.episode_id === episodeId) return '正在播放';
  // 后端按保存的位置续播；时长尚未取得或进度不足 1% 时也不能当成首次播放。
  if (progress && !progress.completed && Number.isFinite(progress.position)
    && progress.position > 0) return '继续播放';
  return '开始播放';
}

export function mergeActiveSessionProgress(
  persistedItems: PlaybackProgressItem[],
  playbackStatus: PlaybackStatusSnapshot | null,
  workId: string,
): PlaybackProgressItem[] {
  const session = playbackStatus?.session;
  if (
    playbackStatus?.status !== 'playing'
    || !session
    || session.work_id !== workId
    || !session.episode_id
    || !Number.isFinite(session.position)
    || !Number.isFinite(session.duration)
    || Number(session.position) < 0
    || Number(session.duration) <= 0
  ) {
    return persistedItems;
  }

  const position = Number(session.position);
  const duration = Number(session.duration);
  const ratio = Math.round(Math.max(0, Math.min(1, position / duration)) * 10_000) / 10_000;
  const existingIndex = persistedItems.findIndex(
    (item) => item.work_id === workId && item.episode_id === session.episode_id,
  );

  if (existingIndex < 0) {
    return [...persistedItems, {
      work_id: workId,
      episode_id: session.episode_id,
      asset_id: session.asset_id,
      position,
      duration,
      ratio,
      completed: false,
      updated_at: session.started_at || '',
      bangumi_synced: false,
      bangumi_error: '',
      manually_unwatched: false,
    }];
  }

  return persistedItems.map((item, index) => index === existingIndex
    ? { ...item, position, duration, ratio }
    : item);
}
