/**
 * B4：媒体导入流程的弹窗与命令区样式合同。
 *
 * 这些断言锁的是"用户实际看到过的缺陷"，因此对着 `src/index.css` 的真实声明
 * 检查，而不是只看组件里有没有 class：
 * 1. `.media-v4-command-row > div` 曾经无条件把按钮容器也变成 2px 单列网格，
 *    导致「增量扫描 / 正在扫描 / 取消扫描」竖排且间距只有 2px —— 选择器必须
 *    排除按钮容器；
 * 2. 弹窗表单的 label/控件/hint 不能继续沿用 Fluent 的 2px 默认间距；
 * 3. 标题与关闭按钮必须同行（不能只依赖 Fluent 默认的 DialogTitle action 网格）；
 * 4. 来源卡弹窗必须是实体可读表面（半透明 raised 会让背后卡片文字透出来）；
 * 5. 扫描进度行必须脱离 MessageBar 自身的多列网格。
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

test('命令区按钮容器不再被通用 div 规则压成单列网格', () => {
  assert.ok(css.includes('.media-v4-command-row > div:not(.media-v4-command-buttons)'))
  assert.match(css, /\.media-v4-command-buttons\s*\{[^}]*display:\s*flex/)
  assert.match(css, /\.media-v4-command-buttons\s*\{[^}]*flex-wrap:\s*wrap/)
})

test('识别弹窗为表单提供 label / 控件 / hint 的竖向节奏', () => {
  assert.match(css, /\.media-v4-recognition-dialog \.fui-Field\s*\{[^}]*display:\s*grid/)
  assert.match(css, /\.media-v4-recognition-dialog \.fui-Field\s*\{[^}]*row-gap:\s*6px/)
  assert.match(css, /\.media-v4-recognition-dialog \.fui-Field__label\s*\{[^}]*margin:\s*0/)
  assert.match(css, /\.media-v4-recognition-dialog \.fui-Field__hint\s*\{[^}]*margin-top:\s*0/)
  // 控件最小高度声明在 Input/Select 组合规则里；只做宽松匹配，避免被无关声明顺序影响。
  assert.match(css, /min-height:\s*38px/)
})

test('识别弹窗标题行把关闭按钮固定在同行右侧', () => {
  assert.match(
    css,
    /\.media-v4-recognition-dialog \.fui-DialogTitle\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto/,
  )
})

test('来源卡弹窗使用实体表面并保持同一套表单节奏', () => {
  assert.match(css, /\.media-v4-source-card-rename-dialog\s*\{[^}]*background:\s*var\(--surface-solid\)/)
  assert.doesNotMatch(
    css,
    /\.media-v4-source-card-rename-dialog\s*\{[^}]*background:\s*var\(--surface-raised\)/,
  )
  assert.match(css, /\.media-v4-source-card-rename-dialog \.fui-Field\s*\{[^}]*row-gap:\s*6px/)
  assert.match(css, /\.media-v4-source-card-rename-dialog \.fui-Field__hint\s*\{[^}]*margin-top:\s*0/)
})

test('扫描进度行脱离 MessageBar 的多列网格', () => {
  assert.match(css, /\.media-v4-inline-scan-status \.fui-MessageBarBody\s*\{[^}]*display:\s*block/)
  assert.match(css, /\.media-v4-inline-scan-status \.media-v4-scan-progress\s*\{[^}]*width:\s*100%/)
})
