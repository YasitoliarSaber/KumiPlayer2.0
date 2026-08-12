import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';

test('主内容区完全交给 WebView 原生滚动，不拦截鼠标滚轮', () => {
  const shell = readFileSync(new URL('../src/components/shell/AppShell.tsx', import.meta.url), 'utf8');
  const legacyHook = new URL('../src/hooks/useSmoothWheelScroll.ts', import.meta.url);

  assert.doesNotMatch(shell, /useSmoothWheelScroll/);
  assert.equal(existsSync(legacyHook), false, '旧的主线程滚轮动画 hook 应删除');
  assert.doesNotMatch(shell, /onWheel=/);
});
