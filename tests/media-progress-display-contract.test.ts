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

test('未分季的剧集不得显示成 S00：未知季按集号展示', () => {
  assert.match(
    progress,
    /episode\.season_number == null \|\| episode\.season_number <= 0[\s\S]{0,220}第 \$\{episode\.episode_number\} 集/,
    '未分季（season_number 为 null/0）必须显示「第 N 集」，不能拼成 S00E…',
  )
})

test('候选分不再写成"匹配度"：它不是匹配百分比', () => {
  assert.doesNotMatch(progress, /匹配度/, '执行进度页不应再用"匹配度"字样')
  assert.doesNotMatch(management, /匹配度/, '选择作品弹窗不应再用"匹配度"字样')
  assert.match(progress, /候选分/)
  assert.match(management, /候选分/)
})
