import { expect, test } from 'vitest'
import { presentScanProgress } from '../../src/lib/scanProgress'

test('OpenList 读取未知总量，旧的已发现总数也不能显示假百分比', () => {
  const result = presentScanProgress({ stage: 'reading_source', processed_count: 3472, total_count: 3472, discovered_count: 3472, current_directory: '动画', directories_pending: 12 })
  expect(result.value).toBeUndefined()
  expect(result.label).toBe('正在读取目录：动画')
  expect(result.detail).toBe('已发现 3472 个媒体文件 · 待读目录 12 个')
})

test('尚未读到视频时显示查找状态和真实目录推进，不制造文件数', () => {
  expect(presentScanProgress({ stage: 'reading_source', discovered_count: 0, directories_completed: 17, directories_pending: 50 }).detail)
    .toBe('已读取 17 个目录 · 正在查找媒体文件 · 待读目录 50 个')
  expect(presentScanProgress({ stage: 'reading_source', discovered_count: 0 }).detail).toBe('正在查找媒体文件')
})

test('解析使用真实条目数量和剩余数量，不把比例 1 当成 1%', () => {
  expect(presentScanProgress({ stage: 'parsing', processed_count: 16, total_count: 32 })).toMatchObject({ value: 0.5, detail: '已识别 16 / 32 个条目 · 剩余 16 个' })
  expect(presentScanProgress({ stage: 'parsing', processed_count: 32, total_count: 32 }).value).toBe(1)
  expect(presentScanProgress({ stage: 'preparing_preview', processed_count: 32, total_count: 32 }).value).toBeUndefined()
})

test('取消不会继续显示正在读取目录', () => {
  expect(presentScanProgress({ stage: 'reading_source', status: 'cancelling', current_directory: '动画' })).toMatchObject({ label: '正在取消扫描', value: undefined })
})
