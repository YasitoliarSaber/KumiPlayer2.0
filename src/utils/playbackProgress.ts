export type PlaybackProgressItem = {
  work_id: string;
  episode_id: string;
  position: number;
  duration: number;
  ratio: number;
  completed: boolean;
  updated_at: string;
  bangumi_synced: boolean;
  bangumi_error: string;
  manually_unwatched: boolean;
};

type ActivePlaybackSession = {
  work_id: string;
  episode_id: string;
  position: number;
  duration: number;
  started_at: string;
};

type PlaybackStatusSnapshot = {
  status: string;
  session: ActivePlaybackSession | null;
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
    || session.position < 0
    || session.duration <= 0
  ) {
    return persistedItems;
  }

  const ratio = Math.round(
    Math.max(0, Math.min(1, session.position / session.duration)) * 10_000,
  ) / 10_000;
  const existingIndex = persistedItems.findIndex(
    (item) => item.work_id === workId && item.episode_id === session.episode_id,
  );

  if (existingIndex < 0) {
    return [...persistedItems, {
      work_id: workId,
      episode_id: session.episode_id,
      position: session.position,
      duration: session.duration,
      ratio,
      completed: false,
      updated_at: session.started_at || '',
      bangumi_synced: false,
      bangumi_error: '',
      manually_unwatched: false,
    }];
  }

  return persistedItems.map((item, index) => index === existingIndex
    ? {
        ...item,
        position: session.position,
        duration: session.duration,
        ratio,
      }
    : item);
}
