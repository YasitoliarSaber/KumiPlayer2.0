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

test('设置页：不得保留会导致文字重叠的绝对定位', () => {
  assert.match(
    block,
    /\.settings-outline-popover-wrap[\s\S]{0,200}position: static !important;/,
    '设置页大纲浮层必须取消绝对定位',
  )
})
