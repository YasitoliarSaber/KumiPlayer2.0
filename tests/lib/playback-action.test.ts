import { expect, test } from 'vitest';
import { resolvePlaybackActionLabel, type PlaybackProgressItem } from '../../src/utils/playbackProgress';

const progress: PlaybackProgressItem = {
  work_id: 'work', episode_id: 'episode', asset_id: 'asset',
  position: 120, duration: 600, completed: false, updated_at: '',
};
const active = {
  status: 'playing',
  session: {
    session_id: 'session', work_id: 'work', episode_id: 'episode', asset_id: 'asset',
    playback_locator: '', status: 'playing',
  },
};

test('真实会话匹配当前作品和剧集时优先显示正在播放', () => {
  expect(resolvePlaybackActionLabel(active, 'work', 'episode', progress)).toBe('正在播放');
  expect(resolvePlaybackActionLabel(active, 'work', 'episode', null)).toBe('正在播放');
});

test('其他作品或其他剧集的会话不会让当前目标显示正在播放', () => {
  expect(resolvePlaybackActionLabel(active, 'other', 'episode', null)).toBe('开始播放');
  expect(resolvePlaybackActionLabel(active, 'work', 'other', null)).toBe('开始播放');
});

test('退出的会话回到继续播放，有进度但未取得时长时也允许续播', () => {
  expect(resolvePlaybackActionLabel({ ...active, status: 'exited' }, 'work', 'episode', progress)).toBe('继续播放');
  expect(resolvePlaybackActionLabel(null, 'work', 'episode', { ...progress, position: 0.1, duration: 0 })).toBe('继续播放');
});

test('零进度、已完成和非法进度不显示继续播放', () => {
  for (const item of [null, { ...progress, position: 0 }, { ...progress, completed: true },
    { ...progress, position: Number.NaN }, { ...progress, position: -1 }]) {
    expect(resolvePlaybackActionLabel(null, 'work', 'episode', item)).toBe('开始播放');
  }
});
