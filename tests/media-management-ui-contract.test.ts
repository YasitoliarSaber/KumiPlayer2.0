import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8')
const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8')
const execution = readFileSync(new URL('../src/components/media/V4ExecutionProgress.tsx', import.meta.url), 'utf8')
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

test('新导入入口始终从来源选择开始，历史执行只由来源卡打开', () => {
  assert.match(page, /onClick=\{startNewImport\}>导入媒体<\/Button>/)
  assert.doesNotMatch(page, /onClick=\{\(\) => setPageMode\('import'\)\}>导入媒体<\/Button>/)
  assert.match(page, /onClick=\{\(\) => void resumeSourceCard\(card\)\}/)
})

test('来源卡只携带来源摘要与标准操作按钮，不渲染作品预览', () => {
  assert.doesNotMatch(page, /card\.work_previews/)
  assert.match(page, /media-v4-source-card-action \$\{card\.can_resume \? 'primary' : 'secondary'\}/)
  assert.match(page, /media-v4-source-card-action \$\{card\.can_resume \? 'secondary' : 'primary'\}/)
})

test('媒体管理的图标使用 SVG 图标组件，不依赖缺失的字体图标', () => {
  assert.doesNotMatch(maintenance, /@fluentui\/react-icons\/fonts/)
  assert.doesNotMatch(execution, /@fluentui\/react-icons\/fonts/)
  assert.match(maintenance, /Checkbox/)
})

test('导入工作台使用全宽单列轨道，空媒体库有紧凑的引导容器', () => {
  assert.match(styles, /\.media-v4-stage-panel\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
  assert.match(styles, /\.media-v4-source-empty\s*\{[^}]*min-height:/s)
})
