import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { hasVisibleWindowChanged } from '../src/components/library/posterGridMetrics.ts';

test('竖版封面在窗口缩放与跨屏切换后仍由固定比例容器统一裁切', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(styles, /\.poster-media-vertical\s*\{[^}]*aspect-ratio:\s*2\s*\/\s*3/s);
  assert.match(styles, /\.poster-media img\s*\{[^}]*position:\s*absolute[^}]*inset:\s*0[^}]*object-fit:\s*cover/s);
  assert.doesNotMatch(styles, /\.poster-media-vertical img\s*\{[^}]*object-fit:\s*contain/s);
});

test('封面网格在滑块、窗口与跨屏尺寸变化后重新测量可用列数', () => {
  const grid = readFileSync(
    new URL('../src/components/library/VirtualizedPosterGrid.tsx', import.meta.url),
    'utf8',
  );

  assert.match(grid, /calculatePosterGridMetrics/);
  assert.match(grid, /useLayoutEffect/);
  assert.match(grid, /window\.addEventListener\('resize', requestMeasure\)/);
  assert.match(grid, /visualViewport\?\.addEventListener\('resize', requestMeasure\)/);
  assert.match(grid, /matchMedia\?\.\(`\(resolution: /);
  assert.match(grid, /observer = new ResizeObserver\(requestMeasure\)[\s\S]*?observer\.observe\(wrapRef\.current\)/);
  assert.match(grid, /onColumnCapacityChange\?\.\(metrics\.columnCapacity\)/);
});

test('逐像素滚动只在虚拟可见行窗口变化时触发 React 更新', () => {
  const layout = { top: 0, rowHeight: 320, viewportHeight: 900, scrollTop: 0 };
  let updates = 0;
  for (let scrollTop = 1; scrollTop <= 2000; scrollTop += 1) {
    if (!hasVisibleWindowChanged(layout, scrollTop)) continue;
    layout.scrollTop = scrollTop;
    updates += 1;
  }

  assert.ok(updates > 0);
  assert.ok(updates < 20, `预期少于 20 次窗口更新，实际为 ${updates}`);
});

test('虚拟海报墙不为每张卡片叠加实时背景模糊', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
  const marker = '/* Poster wall scroll performance */';

  assert.match(styles, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  const performanceLayer = styles.split(marker, 2)[1];
  assert.match(performanceLayer, /\.virtual-poster-grid \.poster-media\s*\{[^}]*backdrop-filter:\s*none\s*!important/s);
  assert.match(performanceLayer, /\.virtual-poster-grid \.rating-badge\s*\{[^}]*backdrop-filter:\s*none\s*!important/s);
});
