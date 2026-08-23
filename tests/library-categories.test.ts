import test from 'node:test';
import assert from 'node:assert/strict';
import type { WorkIndex } from '../src/api/types.ts';
import { categoryWorkCounts, isWorkInLibraryView } from '../src/utils/libraryCategories.ts';

function work(overrides: Partial<WorkIndex>): WorkIndex {
  return {
    work_id: 'work', title: '作品', original_title: '', year: 2026, rating: 0,
    plot: '', genres: [], studios: [], show_type: 'anime_series', media_type: 'tv',
    source: 'local', card_type: 'main_series', poster_path: '', fanart_path: '',
    clearlogo_path: '', dir_path: '', seasons: [], episodes: [], related_works: [], tags: [],
    last_played: null, ...overrides,
  };
}

test('V4 已确认作品只按作品类型进入媒体库分类', () => {
  const series = work();
  assert.equal(isWorkInLibraryView(series, 'seasonal'), false);
  assert.equal(isWorkInLibraryView(series, 'anime_series'), true);
});

test('分类数量遵循当前来源并保持新番番剧互斥', () => {
  const works = [
    work({ work_id: 'seasonal-local' }),
    work({ work_id: 'anime-local' }),
    work({ work_id: 'anime-baidu', source: 'baidu' }),
    work({ work_id: 'movie-local', show_type: 'anime_movie', media_type: 'movie' }),
  ];

  assert.deepEqual(categoryWorkCounts(works, 'local'), {
    seasonal: 0,
    anime_series: 2,
    anime_movie: 1,
    live_series: 0,
    live_movie: 0,
  });
});

test('混合来源卡片计入每个实际来源', () => {
  const works = [work({
    work_id: 'mixed', source: 'pan115', sources: ['pan115', 'local'],
  })];

  assert.equal(categoryWorkCounts(works, 'pan115').anime_series, 1);
  assert.equal(categoryWorkCounts(works, 'local').anime_series, 1);
  assert.equal(categoryWorkCounts(works, 'baidu').seasonal, 0);
});
