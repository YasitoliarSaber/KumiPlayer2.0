import { api } from './client';
import type { ImportPreview, SourcePathValidation, TaskRecord } from './types';
import type { ImportFamily } from './sources';

export interface MediaTreeVersion {
  version_id: string;
  original_name: string;
  archive_path: string;
  created_at: string;
  source_tree_path?: string;
  snapshot_id: string;
  plan_id: string;
  diff_id: string;
  summary: Record<string, unknown>;
  path_validation?: SourcePathValidation;
}

export interface MediaLibraryPreset {
  preset_id: string;
  name: string;
  source: 'pan115' | 'baidu' | 'local' | 'openlist';
  source_root: string;
  import_family: ImportFamily;
  import_scope: '' | 'seasonal';
  update_mode: 'directory_tree' | 'local_scan' | 'openlist_scan';
  /** OpenList 远端选中目录（独立于本地 source_root） */
  remote_locator?: string;
  /** 关联的 Source Catalog source_root（OpenList 来源卡的权威关联） */
  catalog_root_id?: string;
  /** 内容提供商（OpenList 导入显示真实 provider，不再统一显示为 OpenList） */
  provider_id?: 'pan115' | 'baidu' | 'quark' | 'other' | 'local';
  /** 导入方式：openlist_api / directory_tree / local_scan */
  ingest_method?: 'openlist_api' | 'directory_tree' | 'local_scan';
  /** 使用的 OpenList 提供商路由 */
  source_route_id?: string;
  created_at: string;
  updated_at: string;
  current_snapshot_id: string;
  current_plan_id: string;
  current_version_id: string;
  version_count: number;
  work_count: number;
  video_count: number;
  lifecycle_status: 'draft' | 'confirmed' | 'mirrored' | 'needs_attention' | 'ready';
  is_library_indexed: boolean;
  /** OpenList 来源卡：SourceRoot 投影出的识别单元数（不依赖 current_plan_id） */
  openlist_unit_count?: number;
  /** OpenList 来源卡：SourceRoot 投影出的需处理单元数（draft / failed stage） */
  openlist_attention_count?: number;
  scrape_task?: TaskRecord | null;
  review_count?: number;
  versions: MediaTreeVersion[];
}

export interface PresetImportResult {
  preset: MediaLibraryPreset;
  version: MediaTreeVersion;
  preview: ImportPreview;
  diff?: {
    added_count: number;
    missing_count: number;
    moved_count: number;
    renamed_count: number;
    unchanged_count: number;
  };
  reused_preset?: boolean;
  unchanged?: boolean;
  /** RWK-21/22/27：Provider Source Catalog baseline 状态（TXT 导入/更新） */
  baseline?: {
    root_id?: string;
    job_id?: string;
    status: 'baseline_queued' | 'baseline_reused' | 'baseline_failed';
    /** RWK-35：root 级确认身份（多作品一次确认全部 revisions） */
    confirmation_root_id?: string;
    confirmation_generation?: number;
    revision_ids?: string[];
  };
}

export interface PresetDeletePreview {
  preview_id: string;
  preset_id: string;
  preset_name: string;
  source: string;
  archive_version_count: number;
  preserved_generated_media: boolean;
  preserved_library_data: boolean;
  warnings: string[];
  blocked: boolean;
}

export interface PresetDeleteResult {
  preview_id: string;
  status: 'succeeded' | 'partial_failed';
  deleted_preset: boolean;
  deleted_archive_count: number;
  failed: Array<{ path: string; reason: string }>;
  preserved_generated_media: boolean;
  preserved_library_data: boolean;
}

export const mediaPresetsApi = {
  list: () => api.get<{ presets: MediaLibraryPreset[] }>('/api/media-presets'),

  create: (file: File, params: {
    source: 'pan115' | 'baidu';
    sourceRoot?: string;
    importFamily: ImportFamily;
    importScope?: '' | 'seasonal';
  }) => {
    const body = new FormData();
    body.append('tree_file', file);
    body.append('source', params.source);
    body.append('source_root', params.sourceRoot || '');
    body.append('import_family', params.importFamily);
    body.append('import_scope', params.importScope || '');
    return api.form<PresetImportResult>('/api/media-presets', 'POST', body);
  },

  createFromPath: (treePath: string, params: {
    source?: 'pan115' | 'baidu' | '';
    importFamily: ImportFamily;
    importScope?: '' | 'seasonal';
  }) => api.post<PresetImportResult>('/api/media-presets/import-local-tree', {
    tree_path: treePath,
    expected_source: params.source || '',
    import_family: params.importFamily,
    import_scope: params.importScope || '',
  }),

  scanFolder: (params: {
    source: 'baidu';
    sourceRoot: string;
    importFamily: 'anime';
    importScope: 'seasonal';
  }) => api.post<PresetImportResult>('/api/media-presets/scan-folder', {
    source: params.source,
    source_root: params.sourceRoot,
    import_family: params.importFamily,
    import_scope: params.importScope,
  }),

  update: (presetId: string, file: File) => {
    const body = new FormData();
    body.append('tree_file', file);
    return api.form<PresetImportResult>(`/api/media-presets/${presetId}/updates`, 'POST', body);
  },

  updateFromPath: (presetId: string, treePath: string, expectedSource: 'pan115' | 'baidu') =>
    api.post<PresetImportResult>(`/api/media-presets/${presetId}/updates-from-path`, {
      tree_path: treePath,
      expected_source: expectedSource,
    }),

  rebindRoot: (presetId: string, sourceRoot: string) => api.post<PresetImportResult>(
    `/api/media-presets/${presetId}/source-root`,
    { source_root: sourceRoot },
  ),

  revalidate: (presetId: string) => api.post<PresetImportResult>(
    `/api/media-presets/${presetId}/revalidate`,
    {},
  ),

  rescanLocal: (presetId: string) => api.post<PresetImportResult>(
    `/api/media-presets/${presetId}/rescan-local`,
    {},
  ),

  deletePreview: (presetId: string, signal?: AbortSignal) =>
    api.post<PresetDeletePreview>(`/api/media-presets/${presetId}/delete/preview`, {}, { signal }),

  deleteConfirm: (presetId: string, previewId: string) =>
    api.post<PresetDeleteResult>(`/api/media-presets/${presetId}/delete/confirm`, {
      preview_id: previewId,
    }),
};
