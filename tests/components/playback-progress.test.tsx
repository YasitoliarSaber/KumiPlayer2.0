import { expect, test } from 'vitest';
import {
  mergeActiveSessionProgress,
  type PlaybackProgressItem,
} from '../../src/utils/playbackProgress';

const persisted: PlaybackProgressItem = {
  work_id: 'work-1',
  episode_id: 'episode-1',
  position: 60,
  duration: 1200,
  ratio: 0.05,
  completed: false,
  updated_at: '2026-07-23T10:00:00+08:00',
  bangumi_synced: true,
  bangumi_error: '',
  manually_unwatched: false,
};

test('当前 MPV 会话覆盖同一剧集的旧持久化进度', () => {
  const result = mergeActiveSessionProgress(
    [persisted],
    {
      status: 'playing',
      session: {
        work_id: 'work-1',
        episode_id: 'episode-1',
        position: 600,
        duration: 1200,
        started_at: '2026-07-23T10:05:00+08:00',
      },
    },
    'work-1',
  );

  expect(result).toEqual([{
    ...persisted,
    position: 600,
    duration: 1200,
    ratio: 0.5,
  }]);
});

test('没有持久化记录时为当前剧集生成仅用于展示的实时进度', () => {
  const result = mergeActiveSessionProgress(
    [],
    {
      status: 'playing',
      session: {
        work_id: 'work-1',
        episode_id: 'episode-2',
        position: 90,
        duration: 900,
        started_at: '2026-07-23T10:05:00+08:00',
      },
    },
    'work-1',
  );

  expect(result).toEqual([{
    work_id: 'work-1',
    episode_id: 'episode-2',
    position: 90,
    duration: 900,
    ratio: 0.1,
    completed: false,
    updated_at: '2026-07-23T10:05:00+08:00',
    bangumi_synced: false,
    bangumi_error: '',
    manually_unwatched: false,
  }]);
});

test('保留持久化的完成和同步语义字段', () => {
  const result = mergeActiveSessionProgress(
    [{ ...persisted, completed: true, manually_unwatched: true }],
    {
      status: 'playing',
      session: {
        work_id: 'work-1',
        episode_id: 'episode-1',
        position: 20,
        duration: 1200,
        started_at: '2026-07-23T10:05:00+08:00',
      },
    },
    'work-1',
  );

  expect(result[0]).toMatchObject({
    completed: true,
    bangumi_synced: true,
    manually_unwatched: true,
  });
});

test('其他作品或非法会话进度不会覆盖当前详情页', () => {
  const otherWork = mergeActiveSessionProgress(
    [persisted],
    {
      status: 'playing',
      session: {
        work_id: 'work-2',
        episode_id: 'episode-1',
        position: 600,
        duration: 1200,
        started_at: '',
      },
    },
    'work-1',
  );
  const invalidProgress = mergeActiveSessionProgress(
    [persisted],
    {
      status: 'playing',
      session: {
        work_id: 'work-1',
        episode_id: 'episode-1',
        position: Number.NaN,
        duration: 0,
        started_at: '',
      },
    },
    'work-1',
  );

  expect(otherWork).toEqual([persisted]);
  expect(invalidProgress).toEqual([persisted]);
});
