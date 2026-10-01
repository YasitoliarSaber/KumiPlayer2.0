export interface ScanProgressInput {
  status?: string
  cancel_requested?: boolean
  stage?: string
  stage_label?: string
  processed_count?: number
  total_count?: number
  discovered_count?: number
  current_directory?: string
  directories_completed?: number
  directories_pending?: number
}

/** 总量未知的读取阶段不显示百分比；目录数仅指已经发现的目录。 */
export function presentScanProgress(scan: ScanProgressInput) {
  const processed = Math.max(0, scan.processed_count || 0)
  const total = Math.max(0, scan.total_count || 0)
  if (scan.status === 'cancelling' || scan.cancel_requested) {
    return { label: '正在取消扫描', detail: '正在等待当前读取安全结束…', value: undefined }
  }
  if (scan.stage === 'reading_source') {
    const discovered = Math.max(0, scan.discovered_count ?? processed)
    const directories = scan.directories_pending == null ? '' : ` · 待读目录 ${scan.directories_pending} 个`
    const found = discovered > 0 ? `已发现 ${discovered} 个媒体文件` : '正在查找媒体文件'
    const completed = discovered === 0 && scan.directories_completed != null
      ? `已读取 ${scan.directories_completed} 个目录 · ` : ''
    return {
      label: scan.current_directory ? `正在读取目录：${scan.current_directory}` : '正在读取媒体来源',
      detail: `${completed}${found}${directories}`,
      value: undefined,
    }
  }
  if (['parsing', 'recognizing'].includes(scan.stage || '') && total > 0) {
    const completed = Math.min(processed, total)
    return {
      label: scan.stage_label || '正在识别媒体条目',
      detail: `已识别 ${completed} / ${total} 个条目 · 剩余 ${total - completed} 个`,
      value: completed / total,
    }
  }
  return { label: scan.stage_label || '正在准备你的作品', detail: '正在整理来源与作品信息…', value: undefined }
}
