import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

test('「选择正确作品」弹窗必须显示本地文件名与本地标题', () => {
  assert.match(page, /media-v4-metadata-dialog-local-title/, '缺少本地标题行')
  assert.match(page, /media-v4-metadata-dialog-files/, '缺少文件名样本区块')
  assert.match(
    page,
    /workExecutionDetail\(revisionId, workId\)[\s\S]{0,400}file_name/,
    '文件名应来自该作品的执行详情（episodes[].file_name）',
  )
  assert.match(page, /slice\(0, 8\)/, '文件名样本应限量，避免弹窗过长')
})

test('文件名样本有对应样式（等宽、可换行、可滚动）', () => {
  assert.match(css, /\.media-v4-metadata-dialog-files li \{[^}]*monospace/)
  assert.match(css, /\.media-v4-metadata-dialog-files ul \{[^}]*overflow: auto/)
})
