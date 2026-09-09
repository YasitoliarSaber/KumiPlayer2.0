import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8')
const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8')
const execution = readFileSync(new URL('../src/components/media/V4ExecutionProgress.tsx', import.meta.url), 'utf8')
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

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

test('来源卡通过来源根承载扫描与草稿恢复，不展示游离草稿入口', () => {
  assert.match(page, /mediaV4Api\.sourceLibraries\(\)/)
  assert.match(page, /mediaV4Api\.drafts\(\)/)
  assert.doesNotMatch(page, /aria-label="待继续导入"/)
  assert.doesNotMatch(page, /resumeDraft/)
})

test('来源卡恢复为固定方形 GridView 项目，并保留小窗口单列回退', () => {
  assert.match(styles, /\.media-v4-source-library-grid\s*\{[^}]*grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(270px,\s*300px\)\)/s)
  assert.match(styles, /\.media-v4-library-source-card\s*\{[^}]*aspect-ratio:\s*1\s*\/\s*1/s)
  assert.doesNotMatch(styles, /repeat\(auto-fit,\s*minmax\(min\(100%,\s*360px\),\s*1fr\)\)/)
  assert.match(styles, /@media \(max-width: 760px\)\s*\{\s*\.media-v4-source-library-grid\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
})

test('来源卡仅显示当前人话任务并提供直接终止入口', () => {
  assert.match(page, /card\.active_task/)
  assert.match(page, /mediaV4Api\.cancelImport\(/)
  assert.match(page, /终止任务/)
  assert.doesNotMatch(page, /识别草稿与当前扫描不一致/)
})

test('媒体管理的图标使用 SVG 图标组件，不依赖缺失的字体图标', () => {
  assert.doesNotMatch(maintenance, /@fluentui\/react-icons\/fonts/)
  assert.doesNotMatch(execution, /@fluentui\/react-icons\/fonts/)
  assert.doesNotMatch(maintenance, /Checkbox/)
})

test('导入来源采用 4/2/1 响应式网格，并移除重复流程说明', () => {
  assert.match(styles, /\.media-v4-source-options\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s)
  assert.match(styles, /@container media-flow \(max-width: 900px\)\s*\{\s*\.media-v4-source-options\s*\{\s*grid-template-columns:\s*repeat\(2,/s)
  assert.match(styles, /@container media-flow \(max-width: 620px\)\s*\{\s*\.media-v4-source-options\s*\{\s*grid-template-columns:\s*1fr/s)
  assert.doesNotMatch(page, /选择一个媒体来源，检查识别结果，然后建立可播放的媒体库/)
  assert.doesNotMatch(page, /四种入口使用同一套识别规则/)
  assert.doesNotMatch(page, /option\.description/)
})

test('扫描进度跟随当前操作区，而不是漂在步骤导航上方', () => {
  const sourceWorkspace = page.indexOf('media-v4-config-panel media-v4-workspace')
  const scanProgress = page.indexOf('media-v4-scan-progress')
  assert.ok(sourceWorkspace >= 0)
  assert.ok(scanProgress > sourceWorkspace)
})

test('导入工作台使用全宽单列轨道，空媒体库有紧凑的引导容器', () => {
  assert.match(styles, /\.media-v4-stage-panel\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
  assert.match(styles, /\.media-v4-source-empty\s*\{[^}]*min-height:/s)
})

test('第三步总体摘要位于建立媒体库页头右侧，详情展开保留实际刮削结果', () => {
  assert.match(page, /media-v4-execution-header-summary/)
  assert.match(page, /executeProgress\.work_units\.length/)
  assert.match(execution, /刮削结果/)
  assert.match(execution, /季度结构/)
  assert.match(execution, /episode\.scraped_plot/)
  assert.match(styles, /\.media-v4-execute-stage-header\s*\{[^}]*align-items:\s*flex-start/s)
  assert.match(styles, /\.media-v4-execution-header-summary\s*\{[^}]*justify-self:\s*end/s)
})

test('媒体库主操作与危险维护操作使用分组命令栏', () => {
  assert.match(page, /role="toolbar" aria-label="媒体库操作"/)
  assert.match(page, /media-v4-header-secondary-actions/)
  assert.match(page, /media-v4-header-primary-actions/)
  assert.match(styles, /\.media-v4-header-actions\s*\{[^}]*gap:/s)
  assert.match(styles, /\.media-v4-header-primary-actions\s*\{[^}]*border-inline-start:/s)
})

test('来源卡删除确认框具有独立遮罩与不透明实体表面', () => {
  assert.match(page, /backdrop=\{\{ className: 'media-v4-source-card-delete-backdrop' \}\}/)
  assert.match(styles, /\.media-v4-source-card-delete-backdrop\s*\{[^}]*position:\s*fixed[^}]*inset:\s*0[^}]*background:/s)
  assert.match(styles, /\.media-v4-source-card-delete-dialog\s*\{[^}]*z-index:[^}]*border:[^}]*background:\s*var\(--surface-raised\)[^}]*box-shadow:\s*var\(--shadow-flyout\)/s)
})

test('作品墙轮询首次拿到投影摘要时也刷新数据', () => {
  assert.doesNotMatch(app, /if \(lastDigest !== ''\) loadLibrary\(\)/)
  assert.match(app, /loadLibrary\(\{ force: true \}\)/)
})
