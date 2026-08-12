import test from 'node:test';
import assert from 'node:assert/strict';
import type { WorkIndex } from '../src/api/types.ts';
import {
  categoryWorkCounts,
  isWorkInLibraryView,
} from '../src/utils/libraryCategories.ts';
import { formatTrackingScanNotice } from '../src/utils/trackingScanSummary.ts';

function work(overrides: Partial<WorkIndex>): WorkIndex {
  return {
    work_id: 'work', title: '作品', original_title: '', year: 2026, rating: 0,
    plot: '', genres: [], studios: [], show_type: 'anime_series', media_type: 'tv',
    source: 'local', card_type: 'main_series', poster_path: '', fanart_path: '',
    clearlogo_path: '', dir_path: '', seasons: [], episodes: [], related_works: [], tags: [],
    last_played: null, ...overrides,
  };
}

test('追更作品只属于新番，不重复进入番剧', () => {
  const seasonal = work({ import_scope: 'seasonal' });
  const tracking = work({
    tracking: {
      binding_id: 'binding', work_id: 'work', display_title: '作品', logical_source: 'local',
      root_path: 'D:/作品', season_number: 1, series_group: '', tracking_state: 'tracking',
      attention_state: 'ready', last_snapshot_id: '', baseline_plan_id: '', last_scan_at: '',
      last_successful_scan_at: '', last_result: {},
    },
  });

  for (const item of [seasonal, tracking]) {
    assert.equal(isWorkInLibraryView(item, 'seasonal'), true);
    assert.equal(isWorkInLibraryView(item, 'anime_series'), false);
  }
});

test('已完结追更作品退出新番并进入番剧', () => {
  const completed = work({
    import_scope: 'seasonal',
    tracking: {
      binding_id: 'binding', work_id: 'work', display_title: '作品', logical_source: 'local',
      root_path: 'D:/作品', season_number: 1, series_group: '', tracking_state: 'completed',
      attention_state: 'ready', last_snapshot_id: '', baseline_plan_id: '', last_scan_at: '',
      last_successful_scan_at: '', last_result: {},
    },
  });

  assert.equal(isWorkInLibraryView(completed, 'seasonal'), false);
  assert.equal(isWorkInLibraryView(completed, 'anime_series'), true);
});

test('分类数量遵循当前来源并保持新番番剧互斥', () => {
  const works = [
    work({ work_id: 'seasonal-local', import_scope: 'seasonal' }),
    work({ work_id: 'anime-local' }),
    work({ work_id: 'anime-baidu', source: 'baidu' }),
    work({ work_id: 'movie-local', show_type: 'anime_movie', media_type: 'movie' }),
  ];

  assert.deepEqual(categoryWorkCounts(works, 'local'), {
    seasonal: 1,
    anime_series: 1,
    anime_movie: 1,
    live_series: 0,
    live_movie: 0,
  });
});

test('混合来源卡片计入每个实际来源', () => {
  const works = [work({
    work_id: 'mixed', source: 'pan115', sources: ['pan115', 'local'],
    import_scope: 'seasonal',
  })];

  assert.equal(categoryWorkCounts(works, 'pan115').seasonal, 1);
  assert.equal(categoryWorkCounts(works, 'local').seasonal, 1);
  assert.equal(categoryWorkCounts(works, 'baidu').seasonal, 0);
});

test('新番扫描摘要只列出实际新增剧集的作品', () => {
  const notice = formatTrackingScanNotice({
    results: [
      { display_title: '与你相恋到生命尽头', added_episode_count: 2, status: 'succeeded' },
      { display_title: '没有更新的作品', added_episode_count: 0, status: 'succeeded' },
    ],
    waiting_review: 0,
    failed: 0,
  });

  assert.equal(notice, '更新完成：与你相恋到生命尽头新增 2 集');
  assert.doesNotMatch(notice, /没有更新的作品/);
});

test('新番扫描没有新增剧集时不罗列作品', () => {
  const notice = formatTrackingScanNotice({
    results: [
      { display_title: '没有更新的作品', added_episode_count: 0, status: 'succeeded' },
    ],
    waiting_review: 0,
    failed: 0,
  });

  assert.equal(notice, '扫描完成，未发现新增剧集');
});
