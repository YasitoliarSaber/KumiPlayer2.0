import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  buildHomeShowcase,
  selectHomeCategoryWorks,
  selectSessionShowcase,
} from '../src/utils/homeShowcase.ts';

const works = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map((work_id) => ({ work_id }));

test('右侧三个推荐与左侧轮播静态分区', () => {
  const showcase = buildHomeShowcase(works, 5, 3);

  assert.deepEqual(showcase.featured.map((work) => work.work_id), ['a', 'b', 'c', 'd', 'e']);
  assert.deepEqual(showcase.side.map((work) => work.work_id), ['f', 'g', 'h']);
  assert.equal(
    showcase.featured.some((work) => showcase.side.some((side) => side.work_id === work.work_id)),
    false,
  );
});

test('不足四部作品时循环补齐右侧三个槽位', () => {
  const twoWorks = works.slice(0, 2);
  const showcase = buildHomeShowcase(twoWorks, 5, 3);

  assert.deepEqual(showcase.featured.map((work) => work.work_id), ['a']);
  assert.deepEqual(showcase.side.map((work) => work.work_id), ['b', 'b', 'b']);
});

test('只有一部作品时仍然填满全部推荐槽位', () => {
  const showcase = buildHomeShowcase(works.slice(0, 1), 5, 3);

  assert.deepEqual(showcase.featured.map((work) => work.work_id), ['a']);
  assert.equal(showcase.side.length, 3);
  assert.ok(showcase.side.every((work) => work.work_id === 'a'));
});

test('首页右侧推荐不依赖当前左侧轮播项', () => {
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');

  assert.match(home, /buildHomeShowcase\(showcaseWorks/);
  assert.match(home, /selectSessionShowcase\(\s*source,\s*homeDisplayWorks/s);
  assert.doesNotMatch(home, /buildSideShowcase\(showcaseWorks,\s*featured/);
});

test('首页轮播保留已解码图层并通过透明度交叉切换', () => {
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(home, /featuredWorks\.map\(\(work,\s*index\)\s*=>/);
  assert.match(home, /onDecoded=\{\(\)\s*=>\s*markFeatureReady\(index\)\}/);
  assert.match(home, /index === normalizedVisibleFeatureIndex/);
  assert.match(styles, /\.home-feature-primary\s*\{[^}]*opacity:\s*0[^}]*transition:\s*opacity/s);
  assert.match(styles, /\.home-feature-primary\.is-active\s*\{[^}]*opacity:\s*1/s);
  assert.match(styles, /\[data-motion='reduced'\][^{]*\.home-feature-primary/);
});

test('同一次应用会话返回首页时保持原推荐顺序', () => {
  const initial = selectSessionShowcase('session-stability-test', works, 8, () => 0);
  const refreshedWorks = works.map((work) => ({ ...work }));
  const returned = selectSessionShowcase('session-stability-test', refreshedWorks, 8, () => 0.99);

  assert.deepEqual(
    returned.map((work) => work.work_id),
    initial.map((work) => work.work_id),
  );
  assert.notEqual(returned[0], initial[0]);
});

test('首页分类横排隐藏无海报作品并按最近播放排序', () => {
  const categoryWorks = [
    { work_id: 'unwatched', title: '未观看', poster_path: '/poster/unwatched.jpg' },
    { work_id: 'broken', title: '刮削失败', poster_path: '' },
    { work_id: 'older', title: '较早观看', poster_path: '/poster/older.jpg' },
    { work_id: 'latest', title: '最近观看', poster_path: '/poster/latest.jpg' },
  ];
  const history = [
    { work_id: 'latest' },
    { work_id: 'broken' },
    { work_id: 'older' },
  ];

  assert.deepEqual(
    selectHomeCategoryWorks(categoryWorks, history, 'recent', 8).map((work) => work.work_id),
    ['latest', 'older', 'unwatched'],
  );
});

test('首页分类横排跟随分类页排序设置且不改变输入数组', () => {
  const categoryWorks = [
    { work_id: 'b', title: '乙', poster_path: '/poster/b.jpg', rating: 7 },
    { work_id: 'missing', title: '无海报', poster_path: '   ', rating: 10 },
    { work_id: 'a', title: '甲', poster_path: '/poster/a.jpg', rating: 9 },
  ];
  const originalOrder = categoryWorks.map((work) => work.work_id);

  assert.deepEqual(
    selectHomeCategoryWorks(categoryWorks, [], 'ratingDesc', 8).map((work) => work.work_id),
    ['a', 'b'],
  );
  assert.deepEqual(categoryWorks.map((work) => work.work_id), originalOrder);
});
