import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
const marker = '设置页版式归一化（用户反馈"文字重叠"）'
const start = css.indexOf(marker)
const block = start >= 0 ? css.slice(start) : ''

test('设置页：末尾必须有版式归一化覆盖块（否则被历史 !important 规则压回去）', () => {
  assert.ok(start >= 0, 'index.css 末尾缺少设置页归一化块')
  assert.match(block, /\.settings-unified-stack[\s\S]{0,80}gap: 28px !important;/, '区块间距应收敛为常规值')
  assert.match(block, /\.settings-section-head h3[\s\S]{0,120}font-size: 18px !important;/, '区块标题字号应固定')
})

test('设置页：左侧目录必须常驻，弹层气泡不得绝对定位', () => {
  // 用户反馈（2026-09-24）：设置页往下滑就看不到左边导航栏。
  // 历史原因：为修"文字重叠"，末尾把目录包裹层也一并压成 position: static!important，
  // 连带取消了它的常驻（滚动容器是 .app-main）。现在目录必须是 sticky。
  assert.match(
    block,
    /\.settings-outline-popover-wrap\s*\{[\s\S]{0,240}?position:\s*sticky !important;/,
    '设置页左侧目录必须常驻（sticky）',
  )
  assert.match(
    block,
    /\.settings-outline-popover-wrap\s*\{[\s\S]{0,240}?top:\s*\d+px !important;/,
    '常驻偏移必须是固定像素值',
  )
  // 要压的是"浮层气泡"，目录本体不能再被列进 static 列表（回归护栏）。
  const staticRule = block.match(
    /\.settings-shell-settings \.settings-outline,[\s\S]{0,160}?\{\s*position:\s*static !important;/,
  )
  assert.ok(staticRule, '弹层气泡仍需取消绝对定位')
  assert.ok(
    !staticRule![0].includes('settings-outline-popover-wrap'),
    '目录包裹层不得再被压成 static（会导致导航栏随页面滚走）',
  )
})
