import { mediaV4Api } from './mediaV4';
// 详情页保留 V4 重构前的成熟界面，部分管理能力已迁移到 V4 权威数据流。
// 尚未迁移的能力给出真实状态原因，不发送任何 Legacy API 请求。

export const workDetailV4Capabilities = {
  manualScrape: true,
  bangumiBinding: false,
  seasonalManagement: true,
  appendEpisodes: false,
  artworkMutation: true,
  titleMutation: true,
  workDeletion: true,
  sourceSpecificFolders: true,
  mirrorFolder: true,
} as const;

const candidateIdByTmdb = new Map<string, string>();

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
  season_number?: number | null;
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
      workId: string,
      source?: string,
      seasonNumber?: number | null,
      groupType?: string,
    ) => Promise.resolve<ScrapeTarget | null>({
      scrape_target_id: workId,
      source: source || '',
      scrape_title: '',
      scrape_type: groupType === 'movie' ? 'movie' : 'tv',
      scrape_year: null,
      local_year: null,
      season_number: seasonNumber ?? null,
    }),
    searchCandidates: async (targetId: string, query?: string, year?: number) => {
      const result = await mediaV4Api.metadataSearch({
        work_id: targetId,
        query: query || '',
        year: year ?? null,
      });
      const candidates = (result.candidates || []).map((item) => {
        const tmdbId = Number(item.provider_id);
        candidateIdByTmdb.set(`${targetId}:${tmdbId}`, item.candidate_id);
        return {
          scrape_target_id: targetId,
          tmdb_id: tmdbId,
          tmdb_type: item.media_type || 'tv',
          title: item.title,
          original_title: item.original_title || '',
          year: item.year,
          poster_path: '',
          vote_average: 0,
          score: 0,
        };
      });
      return { candidates, search_queries: [query || ''] };
    },
    selectCandidate: async (
      targetId: string,
      tmdbId: number,
      _tmdbType: string,
      _selectedBy?: string,
      _searchQuery?: string,
      _seasonNumber?: number,
      _includeEpisode?: boolean,
      _workId?: string,
      _scope?: 'work' | 'season',
    ) => {
      const candidateId = candidateIdByTmdb.get(`${targetId}:${tmdbId}`);
      if (!candidateId) throw new Error('候选已失效，请重新搜索');
      await mediaV4Api.metadataConfirm({ work_id: targetId, candidate_id: candidateId });
      // metadata/confirm 同步完成刮削并刷新投影；空 task_id 表示无需轮询。
      return { task_id: '', status: 'succeeded' };
    },
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
    scan: async (workId: string, _includeScrape?: boolean) => {
      const result = await mediaV4Api.trackingScanWork(workId);
      return { task_id: '', status: result.status };
    },
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
    deleteWorkPreview: async (workId: string) => {
      const preview = await mediaV4Api.deleteWorkPreview(workId);
      return {
        preview_id: preview.preview_id,
        files: preview.artifact_paths.map((path) => ({ path, kind: 'artifact', exists: true, allowed: true, reason: '' })),
        warnings: ['源视频与挂载盘媒体始终保留；确认后作品从媒体库退出并清理受控镜像/NFO/图片。'],
        blocked: false,
        history_count: preview.playback_count,
        progress_count: preview.playback_count,
        related_reference_count: preview.tracking_count,
      } satisfies DeletePreviewResponse;
    },
    deleteWorkConfirm: async (workId: string, previewId: string) => {
      const preview = await mediaV4Api.deleteWorkPreview(workId);
      const result = await mediaV4Api.deleteWorkConfirm(workId, previewId, preview.digest);
      return {
        status: (result.status === 'completed' ? 'succeeded' : 'failed') as 'succeeded' | 'partial_failed' | 'failed',
        failed: result.artifact_results.filter((item) => item.status === 'failed').map((item) => ({ path: item.path, reason: '删除失败' })),
      };
    },
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
