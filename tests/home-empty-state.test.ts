import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';


test('首页没有作品时只显示添加媒体入口', () => {
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');

  assert.match(home, /displayWorks\.length === 0/);
  assert.match(home, /className="home-library-empty"/);
  assert.match(home, /还没有作品/);
  assert.match(home, /添加媒体/);
  assert.match(home, /onClick=\{goManage\}/);
  assert.doesNotMatch(home, /persistentHomeCategories/);
  assert.doesNotMatch(home, /这个分类会固定保留/);
});

test('当前来源只有无封面作品时首页显示整理提示而不是空白', () => {
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');

  assert.match(home, /homeDisplayWorks\.length === 0/);
  assert.match(home, /首页暂无可展示作品/);
  assert.match(home, /分类页仍会保留这些作品/);
});
