// KumiPlayer 2.0 来源 API

import { api } from './client'
import type { SourceInfo, SourcePathValidation } from './types'
import type { OpenListImportBatch } from './openlist'

export type ImportFamily = 'anime' | 'live'

export const sourcesApi = {
  // 列出来源
  list: () =>
    api.get<{ sources: SourceInfo[] }>('/api/sources'),

  // 获取来源列表（简化版）
  getSources: async () => {
    const result = await api.get<{ sources: SourceInfo[] }>('/api/sources');
    return result.sources || [];
  },

  // 列出项目 samples 目录下的目录树文件
  samples: () =>
    api.get<{
      sample_dir: string
      files: Array<{
        name: string
        path: string
        size: number
        modified_at: number
      }>
    }>('/api/sources/samples'),

  // 解析 txt 来源
  parse: (source: string, inputPath: string, sourceRoot?: string, importFamily?: ImportFamily, importScope?: '' | 'seasonal', autoPipeline = false, autoScrape = false) =>
    api.post<{
      snapshot_id: string
      plan_id: string
      source: string
      file_count: number
      video_count: number
      plan_status: string
      import_family: string
      import_scope?: string
      path_validation?: SourcePathValidation
      task_id?: string
      task_status?: string
    }>(`/api/sources/${source}/parse`, {
      input_path: inputPath,
      source_root: sourceRoot,
      import_family: importFamily,
      import_scope: importScope,
      auto_pipeline: autoPipeline,
      auto_scrape: autoScrape,
    }),

  // 扫描本地
  scanLocal: (rootPath: string, importFamily?: ImportFamily, importScope?: '' | 'seasonal', autoPipeline = false, autoScrape = false) =>
    api.post<{
      snapshot_id: string
      plan_id: string
      source: string
      file_count: number
      video_count: number
      plan_status: string
      import_family: string
      task_id?: string
      task_status?: string
    }>('/api/sources/local/scan', {
      root_path: rootPath,
      import_family: importFamily,
      import_scope: importScope,
      auto_pipeline: autoPipeline,
      auto_scrape: autoScrape,
    }),

  createLocalImportBatch: (rootPath: string, importFamily?: ImportFamily, importScope?: '' | 'seasonal') =>
    api.post<OpenListImportBatch>('/api/sources/local/import-batch', {
      root_path: rootPath,
      import_family: importFamily,
      import_scope: importScope,
    }),

  getLocalImportBatch: (batchId: string) =>
    api.get<OpenListImportBatch>(`/api/sources/local/import-batches/${batchId}`),
}
