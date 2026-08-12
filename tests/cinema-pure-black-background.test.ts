import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

test('Cinema 使用中性纯黑背景且不绘制蓝色环境光', () => {
  const themeBlocks = [...styles.matchAll(/:root\[data-theme='cinema'\]\s*\{([\s\S]*?)\}/g)]
    .map((match) => match[1])
    .filter((block) => block.includes('--mica-c'));
  const theme = themeBlocks.at(-1) || '';
  const root = styles.match(/:root\[data-theme='cinema'\]\s+#root\s*\{([\s\S]*?)\}/)?.[1] || '';
  const shell = styles.match(/:root\[data-theme='cinema'\]\s+\.app-shell,\s*:root\[data-theme='cinema'\]\s+\.app-main\s*\{([\s\S]*?)\}/)?.[1] || '';

  assert.match(theme, /--app-bg:\s*#080808;/);
  assert.match(theme, /--content-shell-bg:\s*#080808;/);
  assert.match(theme, /--sidebar-shell-bg:\s*#0c0c0c;/);
  assert.match(theme, /--surface-solid:\s*#141414;/);
  assert.match(root, /background:\s*#080808\s*!important;/);
  assert.doesNotMatch(root, /radial-gradient|linear-gradient|var\(--accent\)/);
  assert.match(shell, /background:\s*#080808\s*!important;/);
});
