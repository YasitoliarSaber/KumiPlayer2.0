import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('评分徽标不叠加逐卡片实时背景模糊', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
  const match = styles.match(/\.rating-badge\s*\{[^}]*\}/);

  assert.ok(match, '期望找到 .rating-badge 规则块');
  assert.doesNotMatch(match[0], /backdrop-filter/);
});
