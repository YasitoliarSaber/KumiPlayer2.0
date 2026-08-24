import type { PlaybackHistoryItem, PlaybackSession } from '../api/types';

export type PlaybackProgressItem = PlaybackHistoryItem & {
  ratio?: number;
  bangumi_synced?: boolean;
  bangumi_error?: string;
  manually_unwatched?: boolean;
};

type PlaybackStatusSnapshot = {
  status: string;
  session: (PlaybackSession & {
    position?: number;
    duration?: number;
    started_at?: string;
  }) | null;
};

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
