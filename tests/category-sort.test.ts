import test from 'node:test';
import assert from 'node:assert/strict';
import {
  getSortOption,
  toggleSort,
  type SortDimension,
} from '../src/utils/categorySort.ts';

test('切换当前排序维度会翻转方向', () => {
  assert.equal(toggleSort('rating', 'rating'), 'ratingAsc');
  assert.equal(toggleSort('ratingAsc', 'rating'), 'ratingDesc');
  assert.equal(toggleSort('year', 'year'), 'yearAsc');
  assert.equal(toggleSort('yearAsc', 'year'), 'yearDesc');
  assert.equal(toggleSort('title', 'title'), 'titleDesc');
  assert.equal(toggleSort('titleDesc', 'title'), 'title');
});

test('切换其他排序维度使用默认方向', () => {
  const dimensions: SortDimension[] = ['recent', 'title', 'rating', 'year'];
  assert.deepEqual(dimensions.map((dimension) => toggleSort('ratingAsc', dimension)), [
    'recent',
    'title',
    'ratingDesc',
    'yearDesc',
  ]);
});

test('排序菜单只展示维度及当前方向', () => {
  assert.deepEqual(getSortOption('ratingAsc'), {
    dimension: 'rating',
    label: '评分',
    directionLabel: '低到高',
  });
  assert.deepEqual(getSortOption('titleDesc'), {
    dimension: 'title',
    label: '名称',
    directionLabel: 'Z-A',
  });
});
