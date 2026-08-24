import { mediaV4Api } from './mediaV4';
// 详情页保留 V4 重构前的成熟界面，部分管理能力已迁移到 V4 权威数据流。
// 尚未迁移的能力给出真实状态原因，不发送任何 Legacy API 请求。

export const workDetailV4Capabilities = {
  manualScrape: true,
  bangumiBinding: false,
  seasonalManagement: false,
  appendEpisodes: false,
  artworkMutation: true,
  titleMutation: true,
  workDeletion: false,
  sourceSpecificFolders: true,
  mirrorFolder: true,
} as const;

const unavailable = <T>(feature: string): Promise<T> =>
  Promise.reject(new Error(`${feature}尚未接入 V4 数据流`));

const unavailableReason = <T>(feature: string, reason: string): Promise<T> =>
  Promise.reject(new Error(`${feature}：${reason}`));

export interface ScrapeTarget {
  scrape_target_id: string;
  source: string;
  scrape_title: string;
  scrape_type: string;
  scrape_year: number | null;
  local_year: number | null;
}

export interface ScrapeCandidate {
  scrape_target_id: string;
  tmdb_id: number;
  tmdb_type: string;
  title: string;
  original_title: string;
  year: number | null;
  poster_path: string;
  vote_average: number;
  score: number;
  source_meta?: Record<string, unknown>;
}

export interface BangumiMatch {
  work_id: string;
  season_number: number | null;
  subject_id: number;
  subject_name: string;
  subject_name_cn: string;
  episode_map: Record<string, number>;
}

export interface BangumiEpisode {
  episode_id: string;
  season_number: number;
  episode_number: number;
  title: string;
  bangumi_episode_id: number | null;
  synced: boolean;
  synced_at: string;
}

export interface ManualEpisodePreviewItem {
  item_id: string;
  path: string;
  season_number: number | null;
  episode_number: number | null;
  title: string;
  status: 'added' | 'existing' | 'replaced' | 'conflict' | 'unrecognized';
}

export interface DeletePreviewResponse {
  preview_id: string;
  files: Array<{ path: string; kind: string; exists: boolean; allowed: boolean; reason: string }>;
  warnings: string[];
  blocked: boolean;
  history_count: number;
  progress_count: number;
  related_reference_count: number;
}

export const workDetailV4Compatibility = {
  scrape: {
    getTargetByWork: (
      _workId: string,
      _source?: string,
      _seasonNumber?: number | null,
      _groupType?: string,
    ) => unavailable<ScrapeTarget | null>('手动刮削'),
    searchCandidates: (_targetId: string, _query?: string, _year?: number) =>
      unavailable<{ candidates: ScrapeCandidate[]; search_queries: string[] }>('手动刮削'),
    selectCandidate: (
      _targetId: string,
      _tmdbId: number,
      _tmdbType: string,
      _selectedBy?: string,
      _searchQuery?: string,
      _seasonNumber?: number,
      _includeEpisode?: boolean,
      _workId?: string,
      _scope?: 'work' | 'season',
    ) => unavailable<{ task_id: string }>('手动刮削'),
    rerunWorkScrape: async (workId: string) => {
      const result = await mediaV4Api.enqueueWorkScrape(workId);
      return { task_id: result.job_id, status: result.status };
    },
  },
  bangumi: {
    getMatch: (_workId: string, _seasonNumber?: number) => unavailable<BangumiMatch>('Bangumi 作品绑定'),
    syncProgress: (_workId: string, _seasonNumber?: number) => unavailable<Record<string, unknown>>('Bangumi 进度同步'),
    getEpisodes: (_workId: string, _seasonNumber?: number) => unavailable<{
      work_id: string;
      season_number: number | null;
      match: BangumiMatch | null;
      match_season_number: number | null;
      episodes: BangumiEpisode[];
    }>('Bangumi 剧集映射'),
    getCollection: (_workId: string, _seasonNumber?: number) => unavailable<{
      bangumi: Record<string, unknown> | null;
      match_season_number: number | null;
    }>('Bangumi 收藏同步'),
    confirmMatch: (
      _workId: string,
      _subjectId: number,
      _seasonNumber?: number,
      _subjectName?: string,
      _subjectNameCn?: string,
    ) => unavailable<BangumiMatch>('Bangumi 作品绑定'),
    removeMatch: (_workId: string, _seasonNumber?: number) => unavailable<{ ok: boolean }>('Bangumi 作品绑定'),
    setCollection: (_workId: string, _type: number, _seasonNumber?: number) => unavailable<Record<string, unknown>>('Bangumi 收藏同步'),
  },
  tracking: {
    scan: (_workId: string, _includeScrape?: boolean) => unavailable<{ task_id: string; status: string }>('作品追更扫描'),
    uploadArtwork: (_workId: string, _kind: 'poster' | 'fanart' | 'clearlogo', _file: File) =>
      unavailable<{ path: string }>('手动图片管理'),
    restoreArtwork: (_workId: string, _kind: 'poster' | 'fanart' | 'clearlogo') =>
      unavailable<{ restored: boolean }>('手动图片管理'),
    previewEpisodes: (_workId: string, _paths: string[], _seasonNumber: number | null) => unavailable<{
      plan_id: string;
      can_commit: boolean;
      items: ManualEpisodePreviewItem[];
    }>('追加剧集'),
    commitEpisodes: (_workId: string, _planId: string) => unavailable<{ task_id: string; status: string }>('追加剧集'),
  },
  library: {
    setWorkTitle: async (workId: string, title: string) => {
      await mediaV4Api.setWorkTitle(workId, title);
      return { work_id: workId, title };
    },
    restoreWorkTitle: async (workId: string) => {
      await mediaV4Api.restoreWorkTitle(workId);
      return { work_id: workId, restored: true };
    },
    deleteWorkPreview: (_workId: string) => unavailableReason<DeletePreviewResponse>('单个作品删除', '请先通过媒体库维护按来源清理，或等待单作品删除命令接入'),
    deleteWorkConfirm: (_workId: string, _previewId: string) => unavailableReason<{
      status: 'succeeded' | 'partial_failed' | 'failed';
      failed: Array<{ path: string; reason: string }>;
    }>('单个作品删除', '请先通过媒体库维护按来源清理，或等待单作品删除命令接入'),
  },
  artwork: {
    upload: (workId: string, kind: 'poster' | 'fanart' | 'clearlogo', file: File) => {
      const reader = new FileReader();
      return new Promise<{ path: string }>((resolve, reject) => {
        reader.onload = () => {
          const data = String(reader.result ?? '').split(',')[1] ?? '';
          mediaV4Api.uploadWorkArtwork(workId, kind, data).then(resolve, reject);
        };
        reader.onerror = () => reject(new Error('读取图片失败'));
        reader.readAsDataURL(file);
      });
    },
    restore: (workId: string, kind: 'poster' | 'fanart' | 'clearlogo') => mediaV4Api.restoreWorkArtwork(workId, kind),
  },
  folders: {
    open: async (workId: string) => mediaV4Api.workFolder(workId),
  },
};
