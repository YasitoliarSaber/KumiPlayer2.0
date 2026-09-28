import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const progress = readFileSync(
  new URL('../src/components/media/V4ExecutionProgress.tsx', import.meta.url),
  'utf8',
)
const management = readFileSync(
  new URL('../src/pages/MediaManagementPage.tsx', import.meta.url),
  'utf8',
)

test('未分季的剧集不得显示成 S00：未知季/未知集写成未定', () => {
  // STEP-014：季号 null != 第 0 季，集号 null != 0；两者都按真实事实写成「未定」，
  // 不得拼出 S00E…，也不得用占位符把未知集号伪装成 E00 或 E?。
  assert.match(
    progress,
    /episode\.season_number == null[\s\S]{0,240}季号未定/,
    '季号未知时必须显示「季号未定」，不能当作第 0/1 季',
  )
  assert.match(
    progress,
    /episodeNumber == null[\s\S]{0,120}集号未定/,
    '集号未知时必须显示「集号未定」，不能拼出 E00 或 E?',
  )
  assert.doesNotMatch(progress, /S00E/, '未分季不得被拼成 S00E…')
  assert.doesNotMatch(progress, /未编号|未分季（未编号）/, '占位文案已由「季号未定/集号未定」取代')
})

test('候选分不再写成"匹配度"：它不是匹配百分比', () => {
  assert.doesNotMatch(progress, /匹配度/, '执行进度页不应再用"匹配度"字样')
  assert.doesNotMatch(management, /匹配度/, '选择作品弹窗不应再用"匹配度"字样')
  assert.match(progress, /候选分/)
  assert.match(management, /候选分/)
})
