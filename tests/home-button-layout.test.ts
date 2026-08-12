import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';


const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');


test('首页命令使用稳定图标和明确按钮类型', () => {
  assert.match(home, /ArrowRight, ChevronLeft, ChevronRight, FolderPlus, LibraryBig/);
  assert.match(home, /className=\{`home-feature-primary \$\{active \? 'is-active' : ''\}`\}/);
  assert.match(home, /className="home-category-head"/);
  assert.match(home, /className="home-category-link"/);
  assert.match(home, /<ChevronLeft size=\{20\}/);
  assert.match(home, /<ChevronRight size=\{20\}/);
  assert.match(home, /<ArrowRight size=\{15\}/);
  assert.doesNotMatch(home, />查看全部 ›</);
  assert.doesNotMatch(home, />‹</);
  assert.doesNotMatch(home, />›</);
  assert.ok((home.match(/type="button"/g) || []).length >= 6);
});


test('首页轮播按钮不会因激活状态改变整体布局', () => {
  assert.match(styles, /\.home-feature-nav\s*\{[^}]*width:\s*42px[^}]*height:\s*42px[^}]*border-radius:\s*50%/s);
  assert.match(styles, /\.home-feature-dots button\s*\{[^}]*width:\s*28px[^}]*height:\s*28px/s);
  assert.match(styles, /\.home-feature-dots button::before\s*\{/);
  assert.match(styles, /\.home-feature-dots button\.active::before\s*\{/);
  assert.doesNotMatch(styles, /\.home-feature-dots button\.active\s*\{[^}]*width:\s*26px/s);
});


test('首页各类操作具备焦点状态和响应式排布', () => {
  assert.match(styles, /\.home-feature-primary:focus-visible/);
  assert.match(styles, /\.home-feature-tile:focus-visible/);
  assert.match(styles, /\.home-feature-nav:focus-visible/);
  assert.match(styles, /\.home-feature-dots button:focus-visible/);
  assert.match(styles, /\.home-category-link:focus-visible/);
  assert.match(styles, /\.home-category-head\s*\{[^}]*flex-wrap:\s*wrap/s);
  assert.match(styles, /\.home-library-empty-action\s*\{[^}]*white-space:\s*nowrap/s);
  assert.match(styles, /@media \(max-width: 520px\)[\s\S]*?\.home-category-link/);
});
