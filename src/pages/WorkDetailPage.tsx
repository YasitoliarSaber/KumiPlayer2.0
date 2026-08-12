import { useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, ChevronLeft, ChevronRight, Circle, Ellipsis, FolderOpen, FolderSymlink, Grid2X2, Heart, Image, List, Pencil, Play, ScanLine, SlidersHorizontal, Star, Trash2, Upload, X } from 'lucide-react';
import { useLibraryStore } from '../stores/library';
import { useBangumiStore } from '../stores/bangumi';
import { useUiStore, type CategoryKey } from '../stores/ui';
import { systemApi } from '../api/system';
import { scrapeApi, type ScrapeCandidate, type ScrapeTarget } from '../api/scrape';
import { bangumiApi, buildBangumiImageUrl, type BangumiEpisode, type BangumiMatch } from '../api/bangumi';
import { playbackApi } from '../api/playback';
import { cleanDisplayTitle } from '../utils/title';
import { buildAssetUrl } from '../api/assets';
import { trackingApi, type ManualEpisodePreviewItem } from '../api/tracking';
import { tasksApi } from '../api/tasks';
import { libraryApi, type DeletePreviewResponse } from '../api/library';
import { useDismissiblePopover } from '../hooks/useDismissiblePopover';
import { listenForVideoFileDrop } from '../platform/fileDrop';
import DecodedImage from '../components/ui/DecodedImage';
import DetailSeasonPicker from '../components/media/DetailSeasonPicker';
import {
  mergeActiveSessionProgress,
  type PlaybackProgressItem,
} from '../utils/playbackProgress';

const bangumiCollectionTypes = [
  { value: 3, label: '在看' },
  { value: 2, label: '看过' },
  { value: 1, label: '想看' },
  { value: 4, label: '搁置' },
  { value: 5, label: '抛弃' },
];

type AuxiliarySnapshot = {
  scrapeTarget: ScrapeTarget | null;
  bangumiMatch: BangumiMatch | null;
  bangumiMatchSeasonNumber: number | null;
  bangumiEpisodes: BangumiEpisode[];
  bangumiCollection: any;
  playedEpisodeIds: string[];
  progressItems: PlaybackProgressItem[];
  completedEpisodeIds: string[];
  manualUnwatchedEpisodeIds: string[];
  continueEpisodeId: string;
};

type EpisodeContextMenu = {
  episode: any;
  x: number;
  y: number;
} | null;

type PendingPlaybackIntent = {
  workId: string;
  episodeId: string;
} | null;

const CACHE_TTL_MS = 30_000; // 30 seconds

const auxiliaryCache = new Map<string, { data: AuxiliarySnapshot; ts: number }>();

function auxiliaryCacheKey(workId: string, seasonNumber: number | null | undefined, groupType?: string) {
  return `${workId}:${seasonNumber ?? 'all'}:${groupType || 'any'}`;
}

function getCacheEntry(key: string): AuxiliarySnapshot | null {
  const entry = auxiliaryCache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CACHE_TTL_MS) {
    auxiliaryCache.delete(key);
    return null;
  }
  return entry.data;
}

function setCacheEntry(key: string, data: AuxiliarySnapshot) {
  auxiliaryCache.set(key, { data, ts: Date.now() });
}

function seasonOptionKey(season: any) {
  return `${season?.group_type || 'season'}:${Number(season?.season_number ?? 0)}`;
}

function normalizedGroupType(value: string | undefined | null) {
  return value === 'sps' ? 'special' : (value || '');
}

function scrollEpisodeIntoView(
  strip: HTMLElement,
  target: HTMLElement,
  behavior: ScrollBehavior = 'smooth',
) {
  const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
  const centeredLeft = target.offsetLeft - (strip.clientWidth - target.clientWidth) / 2;
  strip.scrollTo({ left: Math.max(0, Math.min(maxScroll, centeredLeft)), behavior });
}

export default function WorkDetailPage() {
  const { selectedWorkId, activeCategory, goCategory, selectedSeasonNumber, selectSeason, rememberWorkSeason, setActiveCategory } = useUiStore();
  const works = useLibraryStore((state) => state.works);
  const peekWorkDetail = useLibraryStore((state) => state.peekWorkDetail);
  const getWorkDetail = useLibraryStore((state) => state.getWorkDetail);
  const openWorkDetail = useLibraryStore((state) => state.openWorkDetail);
  const refreshHistory = useLibraryStore((state) => state.refreshHistory);
  const refreshWork = useLibraryStore((state) => state.refreshWork);
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const updateWorkWatchStatus = useLibraryStore((state) => state.updateWorkWatchStatus);
  const bangumiSessionStatus = useBangumiStore((state) => state.sessionStatus);
  const initialWork = selectedWorkId
    ? peekWorkDetail(selectedWorkId)
    : null;
  const [work, setWork] = useState<any>(() => initialWork);
  const [loading, setLoading] = useState(!initialWork);
  const [showLoadProgress, setShowLoadProgress] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scrapeTarget, setScrapeTarget] = useState<ScrapeTarget | null>(null);
  const [scrapeCandidates, setScrapeCandidates] = useState<ScrapeCandidate[]>([]);
  const [scrapeQuery, setScrapeQuery] = useState('');
  const [scrapeYear, setScrapeYear] = useState('');
  const [scrapeBusy, setScrapeBusy] = useState(false);
  const [scrapeScope, setScrapeScope] = useState<'work' | 'season'>('work');
  const [manualScrapeOpen, setManualScrapeOpen] = useState(false);
  const [bangumiPanelOpen, setBangumiPanelOpen] = useState(false);
  const [bangumiMatch, setBangumiMatch] = useState<BangumiMatch | null>(null);
  const [bangumiMatchSeasonNumber, setBangumiMatchSeasonNumber] = useState<number | null>(null);
  const [bangumiEpisodes, setBangumiEpisodes] = useState<BangumiEpisode[]>([]);
  const [bangumiQuery, setBangumiQuery] = useState('');
  const [bangumiCandidates, setBangumiCandidates] = useState<any[]>([]);
  const [bangumiSearching, setBangumiSearching] = useState(false);
  const [, setPlayedEpisodeIds] = useState<Set<string>>(new Set());
  const [progressByEpisodeId, setProgressByEpisodeId] = useState<Map<string, PlaybackProgressItem>>(new Map());
  const [completedEpisodeIds, setCompletedEpisodeIds] = useState<Set<string>>(new Set());
  const [manualUnwatchedEpisodeIds, setManualUnwatchedEpisodeIds] = useState<Set<string>>(new Set());
  const [continueEpisodeId, setContinueEpisodeId] = useState('');
  const [episodeContextMenu, setEpisodeContextMenu] = useState<EpisodeContextMenu>(null);
  const [bangumiCollection, setBangumiCollectionState] = useState<any>(null);
  const [auxiliaryReady, setAuxiliaryReady] = useState(false);
  const [notice, setNotice] = useState('');
  const [selectedSeasonKey, setSelectedSeasonKey] = useState('');
  // null = auto-detect (grid if >15 episodes, list otherwise); 'list'|'grid' = user override
  const [manualEpisodeView, setManualEpisodeView] = useState<'list' | 'grid' | null>(null);
  const [episodeQuickGridOpen, setEpisodeQuickGridOpen] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [plotExpanded, setPlotExpanded] = useState(false);
  const [titleEditOpen, setTitleEditOpen] = useState(false);
  const [titleInput, setTitleInput] = useState('');
  const [deleteWorkOpen, setDeleteWorkOpen] = useState(false);
  const [deletePreview, setDeletePreview] = useState<DeletePreviewResponse | null>(null);
  const [deleteError, setDeleteError] = useState('');
  const [detailPalette, setDetailPalette] = useState('42 52 62');
  const [backdropMode, setBackdropMode] = useState<'preferred' | 'poster' | 'unavailable'>('preferred');
  const [favorite, setFavorite] = useState(Boolean(initialWork?.watch_status?.favorite));
  const [managementBusy, setManagementBusy] = useState(false);
  const [artworkKind, setArtworkKind] = useState<'poster' | 'fanart' | null>(null);
  const [artworkFile, setArtworkFile] = useState<File | null>(null);
  const [appendOpen, setAppendOpen] = useState(false);
  const [appendPaths, setAppendPaths] = useState('');
  const [appendSeason, setAppendSeason] = useState('1');
  const [appendPlanId, setAppendPlanId] = useState('');
  const [appendItems, setAppendItems] = useState<ManualEpisodePreviewItem[]>([]);
  const [appendCanCommit, setAppendCanCommit] = useState(false);
  const [appendNotice, setAppendNotice] = useState<{
    tone: 'info' | 'success' | 'warning' | 'error';
    message: string;
  } | null>(null);
  const [videoDropActive, setVideoDropActive] = useState(false);
  const [failedCastKeys, setFailedCastKeys] = useState<Set<string>>(new Set());
  const [episodeStripScrollable, setEpisodeStripScrollable] = useState(false);
  const auxiliaryRequestRef = useRef(0);
  const pendingPlaybackIntentRef = useRef<PendingPlaybackIntent>(null);
  const episodeStripRef = useRef<HTMLDivElement>(null);
  const episodeStripSliderRef = useRef<HTMLInputElement>(null);
  const episodeContextMenuRef = useRef<HTMLDivElement>(null);
  const moreMenuRef = useRef<HTMLDivElement>(null);
  const bangumiPanelRef = useRef<HTMLDivElement>(null);
  const bangumiTriggerRef = useRef<HTMLButtonElement>(null);
  const bangumiSearchInputRef = useRef<HTMLInputElement>(null);
  const episodeStripFrameRef = useRef<number | null>(null);
  const autoRevealedEpisodeKeyRef = useRef('');
  const warmedDetailImagesRef = useRef<Set<string>>(new Set());
  const previousBangumiSessionStatusRef = useRef(bangumiSessionStatus);

  const dismissBangumiPanel = () => {
    setBangumiPanelOpen(false);
    window.requestAnimationFrame(() => bangumiTriggerRef.current?.focus());
  };

  useDismissiblePopover(moreMenuOpen, () => setMoreMenuOpen(false), moreMenuRef);
  useDismissiblePopover(bangumiPanelOpen, dismissBangumiPanel, bangumiPanelRef);

  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(''), 2800);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  useEffect(() => {
    if (!bangumiPanelOpen) return;
    const frame = window.requestAnimationFrame(() => bangumiSearchInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [bangumiPanelOpen]);

  useEffect(() => {
    setFailedCastKeys(new Set());
    setPlotExpanded(false);
    setNotice('');
  }, [work?.work_id]);

  useEffect(() => {
    setBackdropMode('preferred');
  }, [work?.work_id, work?.fanart_path, work?.poster_path]);

  useEffect(() => {
    setAppendOpen(false);
    setAppendPaths('');
    setAppendItems([]);
    setAppendPlanId('');
    setAppendCanCommit(false);
    setAppendNotice(null);
  }, [work?.work_id]);

  const previewDroppedEpisodes = async (paths: string[]) => {
    if (!work || paths.length === 0) return;
    const requestedSeason = Number(appendSeason);
    const targetSeason = Number.isFinite(requestedSeason) ? Math.max(0, Math.trunc(requestedSeason)) : 1;
    setAppendOpen(true);
    setAppendPaths(paths.join('\n'));
    setAppendItems([]);
    setAppendPlanId('');
    setAppendCanCommit(false);
    setManagementBusy(true);
    setAppendNotice({ tone: 'info', message: `正在按文件名识别 ${paths.length} 个视频…` });
    try {
      const result = await trackingApi.previewEpisodes(work.work_id, paths, targetSeason);
      setAppendPlanId(result.plan_id);
      setAppendItems(result.items);
      setAppendCanCommit(result.can_commit);
      setAppendNotice({
        tone: result.can_commit ? 'success' : 'warning',
        message: result.can_commit ? '识别完成，请确认追加到当前作品。' : '部分文件无法识别或存在冲突，请检查下方结果。',
      });
    } catch (error) {
      setAppendNotice({ tone: 'error', message: (error as Error).message });
    } finally {
      setManagementBusy(false);
    }
  };

  useEffect(() => {
    if (!work || work.media_type !== 'tv') return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listenForVideoFileDrop((event) => {
      if (disposed) return;
      if (event.type === 'enter') {
        setVideoDropActive(event.paths.length > 0);
        return;
      }
      setVideoDropActive(false);
      if (event.type === 'drop' && event.paths.length > 0) void previewDroppedEpisodes(event.paths);
    }).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [work?.work_id, work?.media_type, appendSeason]);

  useEffect(() => {
    if (!selectedWorkId) return;
    let cancelled = false;
    const progressTimer = window.setTimeout(() => {
      if (!cancelled) setShowLoadProgress(true);
    }, 160);

    const loadWork = async () => {
      setLoading(true);
      setShowLoadProgress(false);
      setError(null);
      try {
        const workData = await getWorkDetail(selectedWorkId);
        if (!cancelled) {
          const initialSeason = resolveInitialSeason(workData, selectedWorkId);
          setWork(workData);
          setFavorite(Boolean(workData.watch_status?.favorite));
          if (useUiStore.getState().activeCategory !== 'seasonal' && isCategoryKey(workData.show_type)) setActiveCategory(workData.show_type);
          selectSeason(initialSeason?.season_number ?? null);
          setSelectedSeasonKey(initialSeason ? seasonOptionKey(initialSeason) : '');
          setBangumiQuery(workData.title || '');
          setScrapeQuery(workData.title || '');
          setScrapeYear(workData.year ? String(workData.year) : '');
          setManualScrapeOpen(false);
          setBangumiPanelOpen(false);
          setMoreMenuOpen(false);
          setScrapeCandidates([]);
          setBangumiCandidates([]);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) {
          window.clearTimeout(progressTimer);
          setLoading(false);
          setShowLoadProgress(false);
        }
      }
    };

    void loadWork();
    return () => {
      cancelled = true;
      window.clearTimeout(progressTimer);
    };
  }, [selectedWorkId, getWorkDetail, selectSeason, setActiveCategory]);

  useEffect(() => {
    if (!work || loading) return;
    setAuxiliaryReady(false);
    setScrapeCandidates([]);
    void loadAuxiliary();
  }, [work, loading, selectedSeasonNumber, selectedSeasonKey]);

  useEffect(() => {
    const year = scrapeTarget?.scrape_year ?? scrapeTarget?.local_year ?? work?.year ?? '';
    setScrapeYear(year ? String(year) : '');
  }, [scrapeTarget?.scrape_target_id, work?.work_id]);

  const applyAuxiliarySnapshot = (snapshot: AuxiliarySnapshot) => {
    const pendingIntent = pendingPlaybackIntentRef.current;
    const nextContinueEpisodeId = pendingIntent && pendingIntent.workId === work?.work_id
      ? pendingIntent.episodeId
      : snapshot.continueEpisodeId;
    setScrapeTarget(snapshot.scrapeTarget);
    setBangumiMatch(snapshot.bangumiMatch);
    setBangumiMatchSeasonNumber(snapshot.bangumiMatchSeasonNumber);
    setBangumiEpisodes(snapshot.bangumiEpisodes);
    setBangumiCollectionState(snapshot.bangumiCollection);
    setPlayedEpisodeIds(new Set(snapshot.playedEpisodeIds));
    setProgressByEpisodeId(buildProgressMap(snapshot.progressItems));
    setCompletedEpisodeIds(new Set(snapshot.completedEpisodeIds));
    setManualUnwatchedEpisodeIds(new Set(snapshot.manualUnwatchedEpisodeIds));
    setContinueEpisodeId(nextContinueEpisodeId);
    setAuxiliaryReady(true);
  };

  const refreshPlaybackSnapshot = async () => {
    if (!work) return null;
    const selectedSeason = findSelectedSeason(work, selectedSeasonKey, selectedSeasonNumber);
    const cacheKey = auxiliaryCacheKey(work.work_id, selectedSeasonNumber, selectedSeason?.group_type);
    const next = getCacheEntry(cacheKey) || {
      scrapeTarget,
      bangumiMatch,
      bangumiMatchSeasonNumber,
      bangumiEpisodes,
      bangumiCollection,
      playedEpisodeIds: [],
      progressItems: [],
      completedEpisodeIds: [],
      manualUnwatchedEpisodeIds: [],
      continueEpisodeId: '',
    };

    const [historyResult, progressResult, statusResult] = await Promise.allSettled([
      playbackApi.getHistory({ work_id: work.work_id, limit: 300 }),
      playbackApi.getProgress(work.work_id),
      playbackApi.getStatus(),
    ]);

    if (historyResult.status === 'fulfilled') {
      const payload = historyResult.value;
      next.playedEpisodeIds = (payload.items || []).map((item: any) => item.episode_id).filter(Boolean);
      next.continueEpisodeId = payload.items?.[0]?.episode_id || '';
      const latestHistoryId = payload.items?.[0]?.history_id || '';
      const globalLatestHistoryId = useLibraryStore.getState().history[0]?.history_id || '';
      if (latestHistoryId && latestHistoryId !== globalLatestHistoryId) {
        void refreshHistory();
      }
    }

    const playbackStatus = statusResult.status === 'fulfilled' ? statusResult.value : null;
    if (progressResult.status === 'fulfilled') {
      next.progressItems = progressResult.value.items || [];
    }
    next.progressItems = mergeActiveSessionProgress(next.progressItems, playbackStatus, work.work_id);
    next.completedEpisodeIds = next.progressItems
      .filter((item) => item.completed)
      .map((item) => item.episode_id);
    next.manualUnwatchedEpisodeIds = next.progressItems
      .filter((item) => item.manually_unwatched)
      .map((item) => item.episode_id);

    if (playbackStatus) {
      const activeSession = playbackStatus.session;
      if (playbackStatus.status === 'playing' && activeSession && activeSession.work_id === work.work_id) {
        next.continueEpisodeId = activeSession.episode_id;
      }
    }

    const pendingIntent = pendingPlaybackIntentRef.current;
    if (pendingIntent && pendingIntent.workId === work.work_id) {
      next.continueEpisodeId = pendingIntent.episodeId;
    }

    setCacheEntry(cacheKey, next);
    setPlayedEpisodeIds(new Set(next.playedEpisodeIds));
    setProgressByEpisodeId(buildProgressMap(next.progressItems));
    setCompletedEpisodeIds(new Set(next.completedEpisodeIds));
    setManualUnwatchedEpisodeIds(new Set(next.manualUnwatchedEpisodeIds));
    setContinueEpisodeId(next.continueEpisodeId);
    return next;
  };

  const loadAuxiliary = async (
    options: { preferCache?: boolean; skipProgressSync?: boolean } = {},
  ) => {
    const requestId = ++auxiliaryRequestRef.current;
    const selectedSeason = findSelectedSeason(work, selectedSeasonKey, selectedSeasonNumber);
    const cacheKey = auxiliaryCacheKey(work.work_id, selectedSeasonNumber, selectedSeason?.group_type);
    if (options.preferCache !== false) {
      const cached = getCacheEntry(cacheKey);
      if (cached) {
        applyAuxiliarySnapshot(cached);
        return;
      }
    }

    const snapshot: AuxiliarySnapshot = {
      scrapeTarget: null,
      bangumiMatch: null,
      bangumiMatchSeasonNumber: null,
      bangumiEpisodes: [],
      bangumiCollection: null,
      playedEpisodeIds: [],
      progressItems: [],
      completedEpisodeIds: [],
      manualUnwatchedEpisodeIds: [],
      continueEpisodeId: '',
    };

    const scrapeTargetSource = normalizedWorkSources(work).length > 1 ? undefined : work.source;

    // 第 1 阶段：并发获取刮削、匹配
    const [scrapeResult, matchResult] = await Promise.allSettled([
      scrapeApi.getTargetByWork(
        work.work_id,
        scrapeTargetSource,
        selectedSeasonNumber ?? null,
        selectedSeason?.group_type,
      ),
      bangumiApi.getMatch(work.work_id, selectedSeasonNumber ?? undefined),
    ]);

    if (scrapeResult.status === 'fulfilled') {
      snapshot.scrapeTarget = scrapeResult.value;
    }

    if (matchResult.status === 'fulfilled') {
      snapshot.bangumiMatch = matchResult.value;
      snapshot.bangumiMatchSeasonNumber = matchResult.value.season_number ?? null;
    }

    // 第 2 阶段：有匹配时执行双向同步（在读取本地进度之前）
    const isConnected = useBangumiStore.getState().sessionStatus === 'connected';
    if (snapshot.bangumiMatch && isConnected && !options.skipProgressSync) {
      try {
        await bangumiApi.syncProgress(work.work_id, selectedSeasonNumber ?? undefined);
      } catch {
        // 同步失败不阻断页面渲染，本地进度保持原样
      }
    }

    // 第 3 阶段：并发获取剧集映射、收藏状态和本地进度
    const [episodesResult, collectionResult, playbackResult] = await Promise.allSettled([
      bangumiApi.getEpisodes(work.work_id, selectedSeasonNumber ?? undefined),
      bangumiApi.getCollection(work.work_id, selectedSeasonNumber ?? undefined),
      refreshPlaybackSnapshot(),
    ]);

    if (episodesResult.status === 'fulfilled') {
      const mapping = episodesResult.value;
      snapshot.bangumiEpisodes = mapping.episodes || [];
      if (mapping.match) {
        snapshot.bangumiMatch = mapping.match;
        snapshot.bangumiMatchSeasonNumber = mapping.match.season_number ?? mapping.season_number ?? null;
      }
    } else {
      snapshot.bangumiEpisodes = [];
    }

    if (collectionResult.status === 'fulfilled') {
      snapshot.bangumiCollection = collectionResult.value.bangumi || null;
      const collMatchSeason = collectionResult.value.match_season_number;
      if (collMatchSeason !== undefined && collMatchSeason !== selectedSeasonNumber) {
        snapshot.bangumiMatchSeasonNumber = collMatchSeason ?? null;
      }
    } else {
      snapshot.bangumiCollection = null;
    }

    const playbackSnapshot = playbackResult.status === 'fulfilled' ? playbackResult.value : null;
    snapshot.playedEpisodeIds = playbackSnapshot?.playedEpisodeIds || [];
    snapshot.progressItems = playbackSnapshot?.progressItems || [];
    snapshot.completedEpisodeIds = playbackSnapshot?.completedEpisodeIds || [];
    snapshot.manualUnwatchedEpisodeIds = playbackSnapshot?.manualUnwatchedEpisodeIds || [];
    snapshot.continueEpisodeId = playbackSnapshot?.continueEpisodeId || '';

    if (requestId !== auxiliaryRequestRef.current) return;
    setCacheEntry(cacheKey, snapshot);
    applyAuxiliarySnapshot(snapshot);
  };

  useEffect(() => {
    const previousStatus = previousBangumiSessionStatusRef.current;
    previousBangumiSessionStatusRef.current = bangumiSessionStatus;
    if (
      !work?.work_id
      || bangumiSessionStatus !== 'connected'
      || previousStatus === 'connected'
    ) return;

    void loadAuxiliary({ preferCache: false });
  }, [bangumiSessionStatus, work?.work_id]);

  useEffect(() => {
    if (!work) return;
    let disposed = false;
    let timer = 0;
    const scheduleNextRefresh = () => {
      timer = window.setTimeout(async () => {
        if (!document.hidden) await refreshPlaybackSnapshot();
        if (!disposed) scheduleNextRefresh();
      }, 3000);
    };
    scheduleNextRefresh();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [work, selectedSeasonNumber, selectedSeasonKey]);

  useEffect(() => {
    if (!episodeContextMenu) return;
    const firstMenuItem = episodeContextMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]');
    firstMenuItem?.focus({ preventScroll: true });
    const close = () => setEpisodeContextMenu(null);
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('click', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [episodeContextMenu]);

  const episodes = useMemo(() => {
    const selectedSeason = (work?.seasons || []).find((season: any) => seasonOptionKey(season) === selectedSeasonKey)
      || (work?.seasons || []).find((season: any) => season.season_number === selectedSeasonNumber)
      || null;
    return (work?.episodes || []).filter((episode: any) => {
      if (!selectedSeason) return selectedSeasonNumber == null;
      return (
        episode.season_number === selectedSeason.season_number &&
        normalizedGroupType(episode.group_type) === normalizedGroupType(selectedSeason.group_type)
      );
    });
  }, [work, selectedSeasonKey, selectedSeasonNumber]);

  // Auto-detect episode view: grid if >15, list otherwise; user manual choice wins
  const autoEpisodeView = episodes.length > 15 ? 'grid' : 'list';
  const effectiveEpisodeView = manualEpisodeView ?? autoEpisodeView;
  const hasEpisodeThumbnails = episodes.some((episode: any) => Boolean(episode.thumb_path));

  const scrollEpisodeStrip = (direction: -1 | 1) => {
    const strip = episodeStripRef.current;
    if (!strip) return;
    strip.scrollBy({ left: direction * Math.max(320, strip.clientWidth * 0.86), behavior: 'smooth' });
  };

  const updateEpisodeStripSlider = (position: number) => {
    const slider = episodeStripSliderRef.current;
    if (!slider) return;
    const clamped = Math.max(0, Math.min(100, position));
    slider.value = String(clamped);
    slider.style.setProperty('--episode-slider-progress', `${clamped}%`);
  };

  const seekEpisodeStrip = (position: number) => {
    const strip = episodeStripRef.current;
    if (!strip) return;
    const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
    const clamped = Math.max(0, Math.min(100, position));
    strip.scrollLeft = maxScroll * (clamped / 100);
    updateEpisodeStripSlider(clamped);
  };

  useEffect(() => {
    const strip = episodeStripRef.current;
    if (!strip || !hasEpisodeThumbnails) return;
    const syncPosition = () => {
      episodeStripFrameRef.current = null;
      const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
      const nextScrollable = maxScroll > 1;
      const nextPosition = maxScroll ? Math.round((strip.scrollLeft / maxScroll) * 100) : 0;
      setEpisodeStripScrollable((current) => current === nextScrollable ? current : nextScrollable);
      updateEpisodeStripSlider(nextPosition);
    };
    const requestSyncPosition = () => {
      if (episodeStripFrameRef.current !== null) return;
      episodeStripFrameRef.current = window.requestAnimationFrame(syncPosition);
    };
    syncPosition();
    strip.addEventListener('scroll', requestSyncPosition, { passive: true });
    window.addEventListener('resize', requestSyncPosition);
    return () => {
      if (episodeStripFrameRef.current !== null) window.cancelAnimationFrame(episodeStripFrameRef.current);
      episodeStripFrameRef.current = null;
      strip.removeEventListener('scroll', requestSyncPosition);
      window.removeEventListener('resize', requestSyncPosition);
    };
  }, [episodes.length, hasEpisodeThumbnails, selectedSeasonKey]);

  const revealEpisodeInStrip = (index: number) => {
    const strip = episodeStripRef.current;
    const target = strip?.children.item(index) as HTMLElement | null;
    if (strip && target) scrollEpisodeIntoView(strip, target);
    setEpisodeQuickGridOpen(false);
  };

  // Reset auto-detect when season changes
  const prevSeasonKey = useRef(selectedSeasonKey);
  useEffect(() => {
    if (prevSeasonKey.current !== selectedSeasonKey) {
      setManualEpisodeView(null);
      prevSeasonKey.current = selectedSeasonKey;
    }
  }, [selectedSeasonKey]);

  const syncedEpisodeIds = useMemo<Set<string>>(() => {
    return new Set((bangumiEpisodes || []).filter((item) => item.synced).map((item) => String(item.episode_id)));
  }, [bangumiEpisodes]);

  const bangumiWatchedLimit = useMemo(() => {
    const status = Number(bangumiCollection?.ep_status ?? 0);
    return Number.isFinite(status) && status > 0 ? status : 0;
  }, [bangumiCollection]);

  const bangumiWatchedEpisodeIds = useMemo<Set<string>>(() => {
    if (!bangumiMatch || bangumiWatchedLimit <= 0) return new Set<string>();
    const matchAppliesToSeason = bangumiMatchSeasonNumber == null || bangumiMatchSeasonNumber === (selectedSeasonNumber ?? null);
    if (!matchAppliesToSeason) return new Set<string>();
    return new Set<string>(
      episodes
        .filter((episode: any) => {
          const number = Number(episode.episode_number || 0);
          return number > 0 && number <= bangumiWatchedLimit;
        })
        .map((episode: any) => String(episode.episode_id)),
    );
  }, [episodes, bangumiMatch, bangumiMatchSeasonNumber, bangumiWatchedLimit, selectedSeasonNumber]);

  const watchedEpisodeIds = useMemo(() => {
    const watched = new Set<string>([...completedEpisodeIds, ...syncedEpisodeIds, ...bangumiWatchedEpisodeIds]);
    for (const episodeId of manualUnwatchedEpisodeIds) watched.delete(episodeId);
    return watched;
  }, [completedEpisodeIds, syncedEpisodeIds, bangumiWatchedEpisodeIds, manualUnwatchedEpisodeIds]);
  const pendingBangumiSyncEpisodeIds = useMemo(() => {
    return episodes
      .filter((episode: any) => {
        const item = progressByEpisodeId.get(episode.episode_id);
        return item?.completed && !item.bangumi_synced && !item.manually_unwatched;
      })
      .map((episode: any) => episode.episode_id)
      .sort();
  }, [episodes, progressByEpisodeId]);
  const pendingBangumiSyncKey = pendingBangumiSyncEpisodeIds.join('|');

  const continueTarget = useMemo(() => {
    return resolveContinueEpisode(episodes, continueEpisodeId, watchedEpisodeIds);
  }, [episodes, continueEpisodeId, watchedEpisodeIds]);
  const continueProgress = continueTarget ? progressByEpisodeId.get(continueTarget.episode_id) : null;
  const continuePercent = progressPercent(continueProgress);

  useEffect(() => {
    if (!work?.work_id || !continueTarget || !episodes.length) return;
    // 同步观看状态后，只在“继续播放目标”变化时定位一次，避免干扰用户之后的手动浏览。
    const revealKey = `${work.work_id}:${selectedSeasonKey}:${continueTarget.episode_id}:${watchedEpisodeIds.size}`;
    if (autoRevealedEpisodeKeyRef.current === revealKey) return;
    let layoutFrame = 0;
    const renderFrame = window.requestAnimationFrame(() => {
      // 等待剧集状态与缩略图横向布局一起提交，再按 episode_id 精确定位。
      layoutFrame = window.requestAnimationFrame(() => {
      const strip = episodeStripRef.current;
      const target = Array.from(strip?.children || []).find(
        (item) => (item as HTMLElement).dataset.episodeId === continueTarget.episode_id,
      ) as HTMLElement | undefined;
      if (!strip || !target) return;
      scrollEpisodeIntoView(strip, target, 'auto');
      autoRevealedEpisodeKeyRef.current = revealKey;
      });
    });
    return () => {
      window.cancelAnimationFrame(renderFrame);
      if (layoutFrame) window.cancelAnimationFrame(layoutFrame);
    };
  }, [work?.work_id, episodes, continueTarget?.episode_id, watchedEpisodeIds.size, selectedSeasonKey]);

  // 统一同步：有匹配时调用双向同步（替换旧的逐集上传循环）
  useEffect(() => {
    if (
      !work
      || !bangumiMatch
      || !pendingBangumiSyncEpisodeIds.length
      || bangumiSessionStatus !== 'connected'
    ) return;

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        await bangumiApi.syncProgress(work.work_id, selectedSeasonNumber ?? undefined);
        if (!cancelled) {
          await loadAuxiliary({ preferCache: false, skipProgressSync: true });
        }
      } catch {
        // 统一同步失败不阻断详情页使用
      }
    }, 800);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    work?.work_id,
    bangumiMatch?.subject_id,
    selectedSeasonNumber,
    bangumiSessionStatus,
    pendingBangumiSyncKey,
  ]);

  if (error && !work) return <CenteredMessage>{error}</CenteredMessage>;
  if (!work) {
    return (
      <div className={`detail-entry-skeleton ${showLoadProgress ? 'is-visible' : ''}`} role="status" aria-label="正在载入作品详情">
        <div className="detail-entry-skeleton-hero" />
        <div className="detail-entry-skeleton-drawer">
          <span />
          <span />
          <span />
        </div>
      </div>
    );
  }

  const seasons = work.seasons || [];
  const currentSeason = findSelectedSeason(work, selectedSeasonKey, selectedSeasonNumber);
  const explicitRelatedWorks = Array.isArray(work.related_works) ? work.related_works : [];
  const relatedWorks = mergeRelatedWorks(
    explicitRelatedWorks,
    explicitRelatedWorks.length > 0 ? [] : buildRelatedWorksFallback(work, works),
  );
  const isMovie = work.media_type === 'movie'
    || work.show_type === 'anime_movie'
    || work.show_type === 'live_movie';
  const isSeries = !isMovie && (work.show_type === 'anime_series' || work.show_type === 'live_series');
  const continueEpisodeTitle = continueTarget
    ? cleanDisplayTitle(continueTarget.title || '', `第 ${continueTarget.episode_number || '?'} 集`)
    : '';
  const continueEpisodeCode = continueTarget ? formatEpisodeCode(continueTarget) : '';
  const continueCompactLabel = !isMovie && isSeries
    ? `${continueEpisodeCode}${continueEpisodeTitle ? `  ${continueEpisodeTitle}` : ''}`
    : work.title;
  const continueHoverLabel = !isMovie && isSeries
    ? `${continueEpisodeCode}${continueEpisodeTitle ? ` · ${continueEpisodeTitle}` : ''}`
    : work.title;
  const visibleCast: Array<{ person: any; castKey: string }> = (Array.isArray(work.cast) ? work.cast : []).slice(0, 14)
    .map((person: any, index: number) => ({ person, castKey: `${person.id || person.name || 'cast'}-${index}` }))
    .filter(({ person, castKey }: { person: any; castKey: string }) => Boolean(person.profile_path) && !failedCastKeys.has(castKey));
  const backdropPath = work.fanart_path || work.poster_path || '';
  const fanartImage = backdropPath ? assetUrl(backdropPath, 'detailBackdrop') : '';
  const posterBackdropImage = work.poster_path ? assetUrl(work.poster_path, 'detailBackdrop') : '';
  const visibleBackdropImage = backdropMode === 'unavailable'
    ? ''
    : backdropMode === 'poster'
      ? posterBackdropImage
      : fanartImage;
  const handleBackdropImageError = () => {
    if (backdropMode === 'preferred' && posterBackdropImage && posterBackdropImage !== fanartImage) {
      setBackdropMode('poster');
      return;
    }
    setBackdropMode('unavailable');
  };
  const clearlogoImage = work.clearlogo_path ? buildAssetUrl(work.clearlogo_path, { kind: 'logo' }) : '';
  const relatedLookup = new Map(works.map((item) => [item.work_id, item]));
  const relatedIds = new Set(relatedWorks.map((item: any) => item.work_id));
  const similarWorks = works
    .filter((item) => item.work_id !== work.work_id && !relatedIds.has(item.work_id))
    .map((item) => ({ item, score: similarityScore(work, item) }))
    .filter(({ score }) => score > 0)
    .sort((left, right) => right.score - left.score || Number(right.item.rating || 0) - Number(left.item.rating || 0))
    .slice(0, 6)
    .map(({ item }) => item);
  const preloadDetailImage = (imageUrl: string) => {
    if (!imageUrl || warmedDetailImagesRef.current.has(imageUrl)) return;
    if (warmedDetailImagesRef.current.size >= 24) {
      const oldest = warmedDetailImagesRef.current.values().next().value;
      if (oldest) warmedDetailImagesRef.current.delete(oldest);
    }
    warmedDetailImagesRef.current.add(imageUrl);
    const image = document.createElement('img');
    image.decoding = 'async';
    image.src = imageUrl;
    void image.decode().catch(() => undefined);
  };
  const prewarmDetailNavigation = (workId: string, imageUrl = '') => {
    preloadDetailImage(imageUrl);
    void getWorkDetail(workId)
      .then((nextWork) => preloadDetailImage(assetUrl(nextWork.fanart_path || nextWork.poster_path || '', 'detailBackdrop')))
      .catch(() => undefined);
  };
  const navigateToWorkDetail = (workId: string) => {
    prewarmDetailNavigation(workId);
    void openWorkDetail(workId);
  };
  const titleTags = mergeTitleTags(work);
  const workSources = normalizedWorkSources(work);
  const sourceFolderEpisodes = workSources
    .map((source) => ({
      source,
      episodeId: work.source_episode_ids?.[source]
        || work.episodes.find((episode: any) => episode.source === source && episode.strm_path)?.episode_id
        || '',
    }))
    .filter(({ episodeId }) => Boolean(episodeId));
  const hasMultipleSources = workSources.length > 1;
  const resolvedBangumiStatusLabel = bangumiMatch
    ? (bangumiMatchSeasonNumber != null && bangumiMatchSeasonNumber !== selectedSeasonNumber)
      ? `Bangumi 已匹配（S${bangumiMatchSeasonNumber}）`
      : 'Bangumi 已匹配'
    : 'Bangumi 未匹配';
  const bangumiStatusLabel = auxiliaryReady ? resolvedBangumiStatusLabel : 'Bangumi 状态载入中';
  const bangumiCollectionLabel = auxiliaryReady && bangumiCollection ? ` · ${collectionLabel(bangumiCollection.type)}` : '';
  const hasDetailTags = workSources.length > 0 || titleTags.length > 0 || (isSeries && seasons.length > 0) || (!isSeries && !!work.year) || work.rating > 0 || !!bangumiStatusLabel;
  const manualScrapeScope = currentSeason
    ? `${currentSeason.label || `第 ${currentSeason.season_number} 季`} · ${normalizedGroupType(currentSeason.group_type) || 'season'}`
    : (isSeries ? '当前作品' : '电影条目');

  const selectDetailSeason = (key: string) => {
    const season = seasons.find((item: any) => seasonOptionKey(item) === key);
    const seasonNumber = season ? Number(season.season_number ?? 0) : null;
    setSelectedSeasonKey(key);
    selectSeason(seasonNumber);
    rememberWorkSeason(work.work_id, { seasonNumber, seasonKey: key });
  };

  const toggleFavorite = async () => {
    const next = !favorite;
    setFavorite(next);
    try {
      const current = work.watch_status || { status: '', note: '' };
      const updated = await libraryApi.setWatchStatus(work.work_id, current.status || '', current.note || '', next);
      setWork({ ...work, watch_status: updated });
      updateWorkWatchStatus(work.work_id, updated);
      setNotice(next ? '已收藏' : '已取消收藏');
    } catch (error) {
      setFavorite(!next);
      setNotice((error as Error).message);
    }
  };

  const extractBackdropPalette = (event: React.SyntheticEvent<HTMLImageElement>) => {
    try {
      const image = event.currentTarget;
      const canvas = document.createElement('canvas');
      canvas.width = 32;
      canvas.height = 18;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      if (!context) return;
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let red = 0; let green = 0; let blue = 0; let count = 0;
      for (let index = 0; index < pixels.length; index += 16) {
        const brightness = pixels[index] + pixels[index + 1] + pixels[index + 2];
        if (brightness < 45 || brightness > 690) continue;
        red += pixels[index]; green += pixels[index + 1]; blue += pixels[index + 2]; count += 1;
      }
      if (count) setDetailPalette(`${Math.round(red / count)} ${Math.round(green / count)} ${Math.round(blue / count)}`);
    } catch {
      setDetailPalette('42 52 62');
    }
  };

  const waitForManagementTask = async (
    taskId: string,
    onProgress?: (message: string) => void,
    timeoutMs = 10 * 60 * 1000,
  ) => {
    const reportProgress = onProgress || setNotice;
    const deadline = Date.now() + timeoutMs;
    let consecutivePollErrors = 0;
    while (Date.now() < deadline) {
      let task: Awaited<ReturnType<typeof tasksApi.get>>;
      try {
        task = await tasksApi.get(taskId);
        consecutivePollErrors = 0;
      } catch (error) {
        consecutivePollErrors += 1;
        if (consecutivePollErrors >= 8) throw error;
        reportProgress('任务仍在后台运行，正在恢复进度连接...');
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        continue;
      }
      reportProgress(task.message || '正在处理');
      if (task.status === 'succeeded') return task;
      if (task.status === 'failed') throw new Error(task.error || '任务失败');
      await new Promise((resolve) => window.setTimeout(resolve, 750));
    }
    throw new Error('任务仍在后台运行，请稍后刷新');
  };

  const reloadCurrentWork = async () => {
    const next = await refreshWork(work.work_id);
    setWork(next);
  };

  const scanCurrentWork = async () => {
    if (work.import_scope !== 'seasonal') return;
    setManagementBusy(true); setMoreMenuOpen(false);
    try {
      const task = await trackingApi.scan(work.work_id, true);
      await waitForManagementTask(task.task_id);
      await reloadCurrentWork();
      setNotice('当前作品扫描完成');
    } catch (error) { setNotice((error as Error).message); }
    finally { setManagementBusy(false); }
  };

  const saveArtwork = async () => {
    if (!artworkKind || !artworkFile) return;
    setManagementBusy(true);
    try {
      await trackingApi.uploadArtwork(work.work_id, artworkKind, artworkFile);
      await reloadCurrentWork();
      setArtworkKind(null); setArtworkFile(null); setNotice('手动图片已保存');
    } catch (error) { setNotice((error as Error).message); }
    finally { setManagementBusy(false); }
  };

  const restoreArtwork = async () => {
    if (!artworkKind) return;
    setManagementBusy(true);
    try {
      await trackingApi.restoreArtwork(work.work_id, artworkKind);
      await reloadCurrentWork();
      setArtworkKind(null); setArtworkFile(null); setNotice('已恢复在线图片');
    } catch (error) { setNotice((error as Error).message); }
    finally { setManagementBusy(false); }
  };

  const openTitleEditor = () => {
    setTitleInput(work.title || '');
    setTitleEditOpen(true);
    setMoreMenuOpen(false);
  };

  const saveWorkTitle = async () => {
    const title = titleInput.trim();
    if (!title) return;
    setManagementBusy(true);
    try {
      await libraryApi.setWorkTitle(work.work_id, title);
      await reloadCurrentWork();
      setTitleEditOpen(false);
      setNotice('作品标题已修改');
    } catch (error) { setNotice((error as Error).message); }
    finally { setManagementBusy(false); }
  };

  const restoreWorkTitle = async () => {
    setManagementBusy(true);
    try {
      await libraryApi.restoreWorkTitle(work.work_id);
      await reloadCurrentWork();
      setTitleEditOpen(false);
      setNotice('已恢复刮削标题');
    } catch (error) { setNotice((error as Error).message); }
    finally { setManagementBusy(false); }
  };

  const openDeleteWork = async () => {
    setMoreMenuOpen(false);
    setDeleteWorkOpen(true);
    setDeletePreview(null);
    setDeleteError('');
    setManagementBusy(true);
    try {
      const preview = await libraryApi.deleteWorkPreview(work.work_id);
      setDeletePreview(preview);
      if (preview.blocked) setDeleteError(preview.warnings.join('；') || '当前作品无法安全删除');
    } catch (error) {
      setDeleteError((error as Error).message);
    } finally {
      setManagementBusy(false);
    }
  };

  const confirmDeleteWork = async () => {
    if (!deletePreview || deletePreview.blocked) return;
    setManagementBusy(true);
    setDeleteError('');
    try {
      const result = await libraryApi.deleteWorkConfirm(work.work_id, deletePreview.preview_id);
      if (result.status !== 'succeeded') {
        throw new Error(result.failed[0]?.reason || '作品删除失败');
      }
      auxiliaryCache.delete(auxiliaryCacheKey(work.work_id, selectedSeasonNumber, currentSeason?.group_type));
      setDeleteWorkOpen(false);
      await loadLibrary({ force: true });
      goCategory(activeCategory === 'seasonal' ? 'seasonal' : work.show_type as CategoryKey);
    } catch (error) {
      setDeleteError((error as Error).message);
    } finally {
      setManagementBusy(false);
    }
  };

  const previewAppendEpisodes = async () => {
    const paths = appendPaths.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    if (!paths.length) return;
    await previewDroppedEpisodes(paths);
  };

  const commitAppendEpisodes = async () => {
    if (!appendPlanId || !appendCanCommit) return;
    setManagementBusy(true);
    setAppendNotice({ tone: 'info', message: '正在保存播放路径并补齐新剧集资料…' });
    try {
      const task = await trackingApi.commitEpisodes(work.work_id, appendPlanId);
      const finishedTask = await waitForManagementTask(
        task.task_id,
        (message) => setAppendNotice({ tone: 'info', message }),
      );
      await reloadCurrentWork();
      setAppendPlanId('');
      setAppendCanCommit(false);
      const taskResult = finishedTask.result as { metadata_status?: string } | null;
      setAppendNotice(taskResult?.metadata_status === 'degraded'
        ? { tone: 'warning', message: '新增剧集已加入媒体库；在线分集资料暂未补齐，缩略图已使用作品背景图。' }
        : { tone: 'success', message: '新增剧集已加入媒体库，播放路径和剧集资料已更新。' });
    } catch (error) { setAppendNotice({ tone: 'error', message: (error as Error).message }); }
    finally { setManagementBusy(false); }
  };

  const closeAppendDialog = () => {
    if (managementBusy) return;
    setAppendOpen(false);
    setAppendPaths('');
    setAppendItems([]);
    setAppendPlanId('');
    setAppendCanCommit(false);
    setAppendNotice(null);
  };

  const handlePlay = async (episodeId?: string) => {
    try {
      // 空目标或刷新空档时不得静默回退到全季第一集，保留已有上下文，无法解析时直接报错。
      const targetEpisodeId = episodeId || continueTarget?.episode_id || '';
      if (!targetEpisodeId) throw new Error('没有可播放的剧集');
      pendingPlaybackIntentRef.current = { workId: work.work_id, episodeId: targetEpisodeId };
      setContinueEpisodeId(targetEpisodeId);
      await playbackApi.play({ work_id: work.work_id, episode_id: targetEpisodeId });
      await refreshHistory();
      await loadAuxiliary({ preferCache: false });
      pendingPlaybackIntentRef.current = null;
    } catch (err) {
      pendingPlaybackIntentRef.current = null;
      alert((err as Error).message);
    }
  };

  const handleEpisodeContextMenu = (event: MouseEvent, episode: any) => {
    event.preventDefault();
    const anchor = event.currentTarget.getBoundingClientRect();
    const activeElement = document.activeElement;
    if (activeElement instanceof HTMLElement && event.currentTarget.contains(activeElement)) {
      activeElement.blur();
    }
    const menuWidth = 168;
    const menuHeight = 128;
    const viewportGap = 10;
    const menuGap = 8;
    const opensUpward = anchor.bottom + menuGap + menuHeight > window.innerHeight - viewportGap;
    const anchoredY = opensUpward
      ? anchor.top - menuHeight - menuGap
      : anchor.bottom + menuGap;
    setEpisodeContextMenu({
      episode,
      x: Math.max(viewportGap, Math.min(event.clientX + 6, window.innerWidth - menuWidth - viewportGap)),
      y: Math.max(viewportGap, Math.min(
        anchoredY,
        window.innerHeight - menuHeight - viewportGap,
      )),
    });
  };

  const markEpisodeCompleted = async (episode: any, completed: boolean) => {
    try {
      await playbackApi.markProgress({
        work_id: work.work_id,
        episode_id: episode.episode_id,
        completed,
      });
      setEpisodeContextMenu(null);
      await loadAuxiliary({ preferCache: false });
    } catch (err) {
      alert((err as Error).message);
    }
  };

  const handleOpenFolder = async (episodeId = '', source = '') => {
    try {
      const result = await systemApi.openFolder(work.work_id, episodeId);
      if (!result.exists) alert(`${source ? `${workSourceLabel(source)}：` : ''}文件夹不存在或挂载未连接`);
    } catch (err) {
      alert(`${source ? `${workSourceLabel(source)}：` : ''}${(err as Error).message}`);
    }
  };

  const handleOpenMirrorFolder = async (episodeId = '', source = '') => {
    try {
      const result = await systemApi.openMirrorFolder(work.work_id, episodeId);
      if (!result.exists) alert(`${source ? `${workSourceLabel(source)}：` : ''}镜像文件夹不存在`);
    } catch (err) {
      alert(`${source ? `${workSourceLabel(source)}：` : ''}${(err as Error).message}`);
    }
  };

  const searchScrapeCandidates = async () => {
    if (scrapeBusy) return;
    setScrapeBusy(true);
    setScrapeCandidates([]);
    setNotice('正在搜索刮削候选...');
    try {
      let target = scrapeTarget;
      if (!target) {
        const selectedSeason = findSelectedSeason(work, selectedSeasonKey, selectedSeasonNumber);
        const scrapeTargetSource = hasMultipleSources ? undefined : work.source;
        target = await scrapeApi.getTargetByWork(
          work.work_id,
          scrapeTargetSource,
          selectedSeasonNumber ?? null,
          selectedSeason?.group_type,
        );
        setScrapeTarget(target);
      }
      if (!target) throw new Error('未找到可刮削目标，请先在设置页生成镜像或重扫媒体库');
      const query = (scrapeQuery || work.title || target.scrape_title || '').trim();
      const parsedYear = scrapeYear.trim() ? Number(scrapeYear.trim()) : undefined;
      if (parsedYear !== undefined && !Number.isFinite(parsedYear)) throw new Error('年份必须是数字，或者留空');
      const result = await scrapeApi.searchCandidates(target.scrape_target_id, query, parsedYear);
      const candidates = result.candidates || [];
      const tried = (result.search_queries || []).slice(0, 6).join(' / ');
      setScrapeCandidates(candidates);
      setNotice(candidates.length
        ? `找到 ${candidates.length} 个候选，选择后将${scrapeScope === 'work' ? '处理整部作品的全部季度' : '只处理当前季度'}`
        : `没有找到候选：${query}${tried ? `。已尝试：${tried}` : ''}`);
    } catch (err) {
      setNotice(`手动刮削失败：${(err as Error).message}`);
    } finally {
      setScrapeBusy(false);
    }
  };

  const selectScrapeCandidate = async (candidate: ScrapeCandidate) => {
    if (scrapeBusy) return;
    setScrapeBusy(true);
    try {
      const task = await scrapeApi.selectCandidate(
        candidate.scrape_target_id,
        candidate.tmdb_id,
        candidate.tmdb_type,
        'manual_replace',
        scrapeQuery || work.title,
        selectedSeasonNumber ?? undefined,
        true,
        work.work_id,
        scrapeScope,
      );
      setScrapeCandidates([]);
      setNotice(scrapeScope === 'work' ? '正在逐季刮削整部作品...' : '正在刮削并刷新当前季度...');
      const finishedTask = await waitForManagementTask(
        task.task_id,
        undefined,
        scrapeScope === 'work' ? 30 * 60 * 1000 : 10 * 60 * 1000,
      );
      await reloadCurrentWork();
      await loadAuxiliary({ preferCache: false });
      if (scrapeScope === 'work') {
        const result = (finishedTask.result || {}) as Record<string, unknown>;
        const manualScraped = result.manual_scraped == null ? 1 : Number(result.manual_scraped || 0);
        const scraped = manualScraped + Number(result.auto_scraped || 0);
        const skipped = Number(result.skipped_existing || 0);
        const pending = Number(result.review_queued || 0);
        const failed = Number(result.failed || 0);
        const remaining = Number(result.remaining_targets || 0);
        setNotice(`整部作品刮削完成：新完成 ${scraped} 季，已有资料 ${skipped} 季${pending ? `，待确认 ${pending} 季` : ''}${failed ? `，失败 ${failed} 季` : ''}${remaining ? `，未处理 ${remaining} 季` : ''}`);
      } else {
        setNotice('刮削完成，作品信息已自动刷新');
      }
    } catch (err) {
      setNotice(`刮削失败：${(err as Error).message}`);
    } finally {
      setScrapeBusy(false);
    }
  };

  const searchBangumi = async () => {
    if (bangumiSearching) return;
    setBangumiSearching(true);
    setBangumiCandidates([]);
    setNotice('正在搜索 Bangumi 候选...');
    try {
      const payload = await bangumiApi.searchSubjects(bangumiQuery || work.title, 8, 0, bangumiSubjectTypes(work.show_type));
      const subjects = extractBangumiSubjects(payload);
      setBangumiCandidates(subjects);
      setNotice(subjects.length ? `找到 ${subjects.length} 个 Bangumi 候选，请确认正确条目` : '没有找到 Bangumi 候选，请换标题重试');
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setBangumiSearching(false);
    }
  };

  const confirmBangumiMatch = async (subject: any) => {
    try {
      const match = await bangumiApi.confirmMatch(
        work.work_id,
        Number(subject.id),
        selectedSeasonNumber ?? undefined,
        subject.name || '',
        subject.name_cn || subject.name || '',
      );
      setBangumiMatch(match);
      setBangumiCandidates([]);
      await loadAuxiliary({ preferCache: false });
      setNotice('Bangumi 条目匹配已确认');
    } catch (err) {
      alert((err as Error).message);
    }
  };

  const removeBangumiMatch = async () => {
    try {
      await bangumiApi.removeMatch(work.work_id, selectedSeasonNumber ?? undefined);
      setBangumiMatch(null);
      setBangumiEpisodes([]);
      setBangumiCollectionState(null);
      const cacheKey = auxiliaryCacheKey(work.work_id, selectedSeasonNumber);
      const cached = getCacheEntry(cacheKey);
      if (cached) {
        setCacheEntry(cacheKey, {
          ...cached,
          bangumiMatch: null,
          bangumiMatchSeasonNumber: null,
          bangumiEpisodes: [],
          bangumiCollection: null,
        });
      }
      setNotice('Bangumi 匹配已移除');
    } catch (err) {
      alert((err as Error).message);
    }
  };

  const setBangumiCollection = async (type: number) => {
    try {
      await bangumiApi.setCollection(work.work_id, type, selectedSeasonNumber ?? undefined);
      await loadAuxiliary({ preferCache: false });
      setNotice('Bangumi 收藏状态已更新');
    } catch (err) {
      alert((err as Error).message);
    }
  };

  return (
    <div
      className={`detail-page detail-classic-page ${isSeries ? 'detail-series-page' : 'detail-movie-page'} ${videoDropActive ? 'is-video-drop-active' : ''}`}
      aria-busy={loading}
      style={{
        '--detail-palette': detailPalette,
      } as any}
    >
      {loading && showLoadProgress && <div className="detail-load-progress" role="status" aria-label="正在补充作品详情"><span /></div>}
      {error && <div className="detail-load-warning" role="alert">详情数据暂时未能更新，当前显示媒体库中的已有信息。</div>}
      {videoDropActive && <div className="detail-video-drop-overlay"><Upload size={34} /><strong>松开即可识别为当前作品的新剧集</strong><span>不会生成新的作品卡片</span></div>}
      <div className="detail-hero">
        {visibleBackdropImage ? (
          <>
            <DecodedImage src={visibleBackdropImage} alt="" className="detail-hero-bg" />
            <DecodedImage src={visibleBackdropImage} alt="" className="detail-hero-art" onLoad={extractBackdropPalette} onError={handleBackdropImageError} />
          </>
        ) : (
          <div className="detail-hero-placeholder" />
        )}
      </div>

      <div className="detail-breadcrumb flex items-center gap-2 text-sm" style={{ pointerEvents: 'auto' }}>
        <button onClick={() => goCategory(activeCategory === 'seasonal' ? 'seasonal' : work.show_type as CategoryKey)} className="hover:underline" style={{ color: 'inherit' }}>
          {activeCategory === 'seasonal' ? '新番' : categoryLabel(work.show_type)}
        </button>
        <span>/</span>
        <span title={work.title}>{work.title}</span>
      </div>

      <section className="detail-hero-copy" aria-label="作品概要" style={{ pointerEvents: 'auto' }}>
        {clearlogoImage ? (
          <DecodedImage src={clearlogoImage} alt={`${work.title} logo`} className="detail-hero-logo" />
        ) : (
          <h1 title={work.title}>{work.title}</h1>
        )}
        {work.original_title && work.original_title !== work.title && <p className="detail-hero-original">{work.original_title}</p>}
        <div className="detail-hero-meta">
          {work.year && <span>{work.year}</span>}
          <span>{categoryLabel(work.show_type)}</span>
          {isSeries && <span>{seasons.length} 季</span>}
          {isSeries && <span>{work.episode_count || work.episodes?.length || 0} 集</span>}
          {work.rating > 0 && <span><Star size={15} fill="currentColor" /> {work.rating.toFixed(1)}</span>}
          {work.certification && (
            <span className="detail-certification" title={`分级地区：${work.certification_country || '未知'}`}>
              {work.certification_country && <b>{work.certification_country}</b>}
              {work.certification}
            </span>
          )}
          {work.import_scope === 'seasonal' && <span>追更中</span>}
        </div>
        {hasDetailTags && (
          <div className="detail-hero-tags">
            {workSources.map((source) => <Tag strong key={source}>{workSourceLabel(source, work)}</Tag>)}
            {titleTags.slice(0, 5).map((tag: string) => <Tag key={tag}>{tag}</Tag>)}
            <button
              ref={bangumiTriggerRef}
              type="button"
              className={`detail-sync-status ${auxiliaryReady && bangumiMatch ? 'matched' : auxiliaryReady ? '' : 'loading'}`}
              disabled={!auxiliaryReady}
              aria-busy={!auxiliaryReady}
              aria-expanded={bangumiPanelOpen}
              aria-haspopup="dialog"
              aria-controls="detail-bangumi-panel"
              onClick={() => setBangumiPanelOpen((open) => !open)}
            >
              {auxiliaryReady && bangumiMatch ? <CheckCircle2 size={14} /> : <Circle size={14} />}
              {bangumiStatusLabel}{bangumiCollectionLabel}
            </button>
          </div>
        )}
        {work.plot && (
          <button
            type="button"
            className={`detail-hero-plot ${plotExpanded ? 'is-expanded' : ''}`}
            aria-expanded={plotExpanded}
            aria-label={plotExpanded ? '收起作品简介' : '展开完整作品简介'}
            title={plotExpanded ? '点击收起简介' : '点击展开完整简介'}
            onClick={() => setPlotExpanded((expanded) => !expanded)}
          >
            {work.plot}
          </button>
        )}
        <div className="detail-hero-actions">
          <div className="detail-play-stack">
            <button
              className={`detail-action-btn primary detail-continue-btn ${continuePercent > 0 ? 'has-progress' : ''}`}
              style={{ '--continue-progress': `${continuePercent}%` } as CSSProperties}
              onClick={() => handlePlay()}
            >
              {continuePercent > 0 && (
                <span
                  className="detail-action-progress"
                  role="progressbar"
                  aria-label="观看进度"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={continuePercent}
                />
              )}
              <Play className="detail-continue-icon" size={18} fill="currentColor" />
              <span className="detail-continue-copy">
                <strong>开始播放</strong>
                {continueTarget && (
                  <small>{continueCompactLabel}</small>
                )}
              </span>
              <span className="detail-continue-hover-state" aria-hidden="true">
                {continueTarget && (
                  <span className="detail-continue-hover-copy">
                    {continueHoverLabel}
                  </span>
                )}
                {continuePercent > 0 && (
                  <span className="detail-continue-percent">
                    {`${continuePercent}%`}
                  </span>
                )}
              </span>
            </button>
          </div>
          <button className={`detail-favorite-btn ${favorite ? 'active' : ''}`} onClick={toggleFavorite} aria-label={favorite ? '取消收藏' : '收藏'} title={favorite ? '取消收藏' : '收藏'}>
            <Heart size={24} fill={favorite ? 'currentColor' : 'none'} />
          </button>
          <div className="detail-more-wrap" ref={moreMenuRef}>
            <button className="detail-more-trigger" onClick={() => setMoreMenuOpen((open) => !open)} aria-label="更多操作" aria-expanded={moreMenuOpen}><Ellipsis size={24} /></button>
            {moreMenuOpen && <div className="detail-more-menu" role="menu">
              <button role="menuitem" onClick={openTitleEditor}><Pencil size={16} />修改作品标题</button>
              {work.import_scope === 'seasonal' && <button role="menuitem" onClick={scanCurrentWork} disabled={managementBusy}><ScanLine size={16} />扫描当前作品</button>}
              <button role="menuitem" onClick={() => { setAppendOpen(true); setMoreMenuOpen(false); }}><Upload size={16} />追加剧集</button>
              <button role="menuitem" onClick={() => { setArtworkKind('fanart'); setMoreMenuOpen(false); }}><Image size={16} />更换背景图</button>
              {hasMultipleSources ? sourceFolderEpisodes.map(({ source, episodeId }) => (
                <button key={`video-${source}`} role="menuitem" onClick={() => { setMoreMenuOpen(false); void handleOpenFolder(episodeId, source); }}><FolderOpen size={16} />打开{workSourceLabel(source)}文件夹</button>
              )) : (
                <button role="menuitem" onClick={() => { setMoreMenuOpen(false); void handleOpenFolder(); }}><FolderOpen size={16} />打开视频文件夹</button>
              )}
              {hasMultipleSources ? sourceFolderEpisodes.map(({ source, episodeId }) => (
                <button key={`mirror-${source}`} role="menuitem" onClick={() => { setMoreMenuOpen(false); void handleOpenMirrorFolder(episodeId, source); }}><FolderSymlink size={16} />打开{workSourceLabel(source)}镜像文件夹</button>
              )) : (
                <button role="menuitem" onClick={() => { setMoreMenuOpen(false); void handleOpenMirrorFolder(); }}><FolderSymlink size={16} />打开镜像文件夹</button>
              )}
              <button role="menuitem" onClick={() => {
                setScrapeScope('work');
                setManualScrapeOpen((open) => !open);
                setMoreMenuOpen(false);
              }}><SlidersHorizontal size={16} />手动刮削</button>
              <button className="danger" role="menuitem" onClick={openDeleteWork}><Trash2 size={16} />删除该作品</button>
            </div>}
          </div>
        </div>
        {notice && createPortal(
          <div className="detail-action-toast" role="status" aria-live="polite">{notice}</div>,
          document.body,
        )}
      </section>

      <div className="detail-content-drawer space-y-7">
      <div className="detail-main-grid">
        <section className="detail-info min-w-0 space-y-5">
          {manualScrapeOpen && createPortal(
            <div className="detail-manual-scrape-panel">
              <div className="detail-manual-scrape-head">
                <div>
                  <strong>手动刮削</strong>
                  <span>{scrapeScope === 'work'
                    ? `整部作品${isSeries && seasons.length ? ` · ${seasons.length} 个季度` : ''}`
                    : manualScrapeScope}</span>
                </div>
                {scrapeTarget && (
                  <span title={scrapeTarget.scrape_title}>{scrapeTarget.scrape_type === 'movie' ? '电影' : '剧集'} · {scrapeTarget.scrape_title}</span>
                )}
                <button type="button" className="detail-tool-close" onClick={() => setManualScrapeOpen(false)} aria-label="关闭手动刮削"><X size={18} /></button>
              </div>
              {isSeries && (
                <div className="detail-manual-scrape-scope" role="group" aria-label="刮削范围">
                  <button
                    type="button"
                    className={scrapeScope === 'work' ? 'active' : ''}
                    aria-pressed={scrapeScope === 'work'}
                    onClick={() => setScrapeScope('work')}
                    disabled={scrapeBusy}
                  >
                    <strong>整部作品</strong>
                    <span>默认处理卡片内全部季度</span>
                  </button>
                  <button
                    type="button"
                    className={scrapeScope === 'season' ? 'active' : ''}
                    aria-pressed={scrapeScope === 'season'}
                    onClick={() => setScrapeScope('season')}
                    disabled={scrapeBusy}
                  >
                    <strong>当前季度</strong>
                    <span>{manualScrapeScope}</span>
                  </button>
                </div>
              )}
              <div className="detail-manual-scrape-row">
                <input
                  value={scrapeQuery}
                  onChange={(event) => setScrapeQuery(event.target.value)}
                  className="settings-input min-w-0 flex-1"
                  placeholder="搜索词"
                />
                <input
                  value={scrapeYear}
                  onChange={(event) => setScrapeYear(event.target.value.replace(/[^\d]/g, '').slice(0, 4))}
                  className="settings-input detail-year-input"
                  inputMode="numeric"
                  placeholder="年份"
                />
                <button className="detail-action-btn" onClick={searchScrapeCandidates} disabled={scrapeBusy}>
                  {scrapeBusy ? '搜索中...' : '搜索候选'}
                </button>
              </div>
              {scrapeCandidates.length > 0 && (
                <CandidatePanel
                  title="刮削候选"
                  items={scrapeCandidates}
                  getTitle={(item) => `${item.title}${item.year ? ` · ${item.year}` : ''}`}
                  getMeta={(item) => `${item.tmdb_type === 'movie' ? '电影' : '剧集'} · ${item.vote_average || 0} 分 · 匹配 ${Math.round(item.score || 0)}`}
                  getImage={(item) => candidateImageUrl(item)}
                  onSelect={selectScrapeCandidate}
                />
              )}
            </div>
          , document.body)}

          {bangumiPanelOpen && createPortal(
            <div className="detail-tool-overlay">
              <div
                ref={bangumiPanelRef}
                id="detail-bangumi-panel"
                className="detail-bangumi-panel"
                role="dialog"
                aria-modal="true"
                aria-labelledby="detail-bangumi-panel-title"
              >
                <div className="detail-bangumi-panel-head">
                  <div>
                    <h2 id="detail-bangumi-panel-title">Bangumi 同步{selectedSeasonNumber ? ` · 第 ${selectedSeasonNumber} 季` : ''}</h2>
                    <span>{bangumiMatch
                      ? bangumiMatchSeasonNumber != null && bangumiMatchSeasonNumber !== selectedSeasonNumber
                        ? `已匹配（来自第 ${bangumiMatchSeasonNumber} 季的匹配）`
                        : '已匹配条目'
                      : '搜索并确认条目后同步收藏和进度'}</span>
                  </div>
                  <strong className={bangumiMatch ? 'matched' : ''}>{bangumiMatch ? '已匹配' : '未匹配'}</strong>
                  <button type="button" className="detail-tool-close" onClick={dismissBangumiPanel} aria-label="关闭 Bangumi 同步"><X size={18} /></button>
                </div>
                <div className="detail-bangumi-current">
                  <span>匹配条目</span>
                  <strong>{bangumiMatch ? bangumiMatch.subject_name_cn || bangumiMatch.subject_name : '请先搜索并确认'}</strong>
                  {bangumiMatch && bangumiMatchSeasonNumber != null && bangumiMatchSeasonNumber !== selectedSeasonNumber && (
                    <small style={{ color: 'var(--warning, #f59e0b)' }}>
                      ⚠ 该匹配属于第 {bangumiMatchSeasonNumber} 季，请为当前季搜索匹配以启用划销
                    </small>
                  )}
                  {bangumiCollection && (
                    <small>
                      Bangumi 当前：{collectionLabel(bangumiCollection.type)}
                      {typeof bangumiCollection.ep_status === 'number' ? ` · 看到 ${bangumiCollection.ep_status} 集` : ''}
                    </small>
                  )}
                </div>
                <div className="settings-inline">
                  <input ref={bangumiSearchInputRef} value={bangumiQuery} onChange={(e) => setBangumiQuery(e.target.value)} className="settings-input min-w-0 flex-1" placeholder="Bangumi 条目名" />
                  <button className="settings-ghost-btn" onClick={searchBangumi} disabled={bangumiSearching}>
                    {bangumiSearching ? '搜索中' : '搜索'}
                  </button>
                </div>
                {bangumiSearching && (
                  <div className="candidate-loading" role="status">
                    <span />
                    <strong>正在搜索候选</strong>
                  </div>
                )}
                {!bangumiSearching && bangumiCandidates.length > 0 && (
                  <CandidatePanel
                    title="Bangumi 候选"
                    items={bangumiCandidates}
                    getTitle={(item) => item.name_cn || item.name || `Subject ${item.id}`}
                    getMeta={(item) => `${item.date || ''}${item.platform ? ` · ${item.platform}` : ''}`}
                    getImage={(item) => bangumiSubjectImageUrl(item)}
                    onSelect={confirmBangumiMatch}
                  />
                )}
                <div className="settings-inline detail-bangumi-status-row">
                  <select className="settings-input min-w-0 flex-1" defaultValue="" onChange={(event) => event.target.value && void setBangumiCollection(Number(event.target.value))}>
                    <option value="">{bangumiCollection ? `当前：${collectionLabel(bangumiCollection.type)}` : '修改收藏状态'}</option>
                    {bangumiCollectionTypes.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </select>
                  <button className="settings-ghost-btn" onClick={removeBangumiMatch} disabled={!bangumiMatch}>移除</button>
                </div>
              </div>
            </div>
          , document.body)}
        </section>
      {isSeries && episodes.length > 0 && (
        <section className="detail-episode-section space-y-4">
          <div className="detail-episode-head flex flex-wrap items-center justify-between gap-3">
            <div className="detail-episode-heading">
              <h2 className="text-xl font-semibold" style={{ color: 'var(--text)' }}>剧集列表</h2>
              {seasons.length > 0 && (
                <DetailSeasonPicker
                  seasons={seasons.map((season: any) => ({
                    key: seasonOptionKey(season),
                    label: season.label || `第 ${season.season_number} 季`,
                  }))}
                  selectedKey={selectedSeasonKey}
                  onSelect={selectDetailSeason}
                />
              )}
            </div>
            <div className="detail-episode-toolbar" role="toolbar" aria-label="剧集浏览工具">
              {hasEpisodeThumbnails ? (
                <div className="detail-episode-pager" aria-label="剧集翻页">
                  <button type="button" className="detail-episode-tool-btn" onClick={() => scrollEpisodeStrip(-1)} aria-label="上一组剧集" title="上一组剧集"><ChevronLeft size={18} /></button>
                  <input
                    ref={episodeStripSliderRef}
                    className="modern-range episode-strip-slider"
                    type="range"
                    min="0"
                    max="100"
                    defaultValue="0"
                    onInput={(event) => seekEpisodeStrip(Number(event.currentTarget.value))}
                    disabled={!episodeStripScrollable}
                    aria-label="拖动定位剧集"
                    title="拖动定位剧集"
                  />
                  <button type="button" className="detail-episode-tool-btn" onClick={() => scrollEpisodeStrip(1)} aria-label="下一组剧集" title="下一组剧集"><ChevronRight size={18} /></button>
                  <button type="button" className="detail-episode-tool-btn" onClick={() => setEpisodeQuickGridOpen(true)} aria-label="快速选集" title="快速选集"><Grid2X2 size={17} /></button>
                </div>
              ) : (
                <>
                  <button type="button" className={`detail-episode-tool-btn detail-episode-view-btn ${effectiveEpisodeView === 'list' ? 'active' : ''}`} onClick={() => setManualEpisodeView('list')} aria-label="列表视图" title="列表视图"><List size={17} /></button>
                  <button type="button" className={`detail-episode-tool-btn detail-episode-view-btn ${effectiveEpisodeView === 'grid' ? 'active' : ''}`} onClick={() => setManualEpisodeView('grid')} aria-label="网格视图" title="网格视图"><Grid2X2 size={17} /></button>
                </>
              )}
            </div>
          </div>
          {bangumiPanelOpen && bangumiMatch && bangumiMatchSeasonNumber != null && bangumiMatchSeasonNumber !== (selectedSeasonNumber ?? null) && (
            <div className="flex items-center gap-2 rounded-lg px-4 py-2 text-sm" style={{ background: 'color-mix(in srgb, var(--warning, #f59e0b) 12%, transparent)', color: 'var(--warning, #f59e0b)', border: '1px solid color-mix(in srgb, var(--warning, #f59e0b) 25%, transparent)' }}>
              ⚠ 该匹配属于第 {bangumiMatchSeasonNumber} 季，当前季的划销不会生效。请在 Bangumi 面板中为本季搜索匹配。
            </div>
          )}
          {bangumiPanelOpen && !bangumiMatch && (
            <div className="flex items-center gap-2 rounded-lg px-4 py-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              连接 Bangumi 后可自动标记已看集数，点击上方「Bangumi 同步」标签页搜索匹配。
            </div>
          )}
          <div
            ref={episodeStripRef}
            className={`detail-episode-grid ${hasEpisodeThumbnails ? 'thumbnail-strip' : effectiveEpisodeView === 'grid' ? 'grid-view' : 'list-view'}`}
          >
            {episodes.map((episode: any) => {
              const rawEpisodeTitle = episode.title || `第 ${episode.episode_number} 集`;
              const episodeTitle = cleanDisplayTitle(rawEpisodeTitle, `第 ${episode.episode_number} 集`);
              const isWatched = watchedEpisodeIds.has(episode.episode_id);
              const isCurrent = continueTarget?.episode_id === episode.episode_id;
              const previewImage = episode.thumb_path ? assetUrl(episode.thumb_path, 'episode') : fanartImage;
              return (
                <div
                  key={episode.episode_id}
                  data-episode-id={episode.episode_id}
                  className={`episode-item ${isWatched ? 'watched' : ''} ${isCurrent ? 'current' : ''}`}
                  onContextMenu={(event) => handleEpisodeContextMenu(event, episode)}
                >
                  <button
                    onClick={() => handlePlay(episode.episode_id)}
                    className="episode-button min-w-0 flex flex-1 items-center gap-3 text-left"
                    aria-label={`${isWatched ? '已看完，' : ''}播放第 ${episode.episode_number} 集：${episodeTitle}`}
                  >
                    <span className="episode-thumb" aria-hidden="true">
                      {previewImage ? (
                        <DecodedImage
                          src={previewImage}
                          alt=""
                          onError={(event) => {
                            if (fanartImage && event.currentTarget.src !== fanartImage) {
                              event.currentTarget.onerror = null;
                              event.currentTarget.src = fanartImage;
                            } else {
                              event.currentTarget.style.display = 'none';
                            }
                          }}
                        />
                      ) : <span><Play size={22} /></span>}
                    </span>
                    <span className="episode-card-copy">
                    <span className="text-base font-semibold tabular-nums" style={{ color: 'var(--text-muted)' }}>
                      {String(episode.episode_number).padStart(2, '0')}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm" style={{ color: 'var(--text)' }} title={rawEpisodeTitle}>
                      {episodeTitle}
                    </span>
                    {episode.source && (work.sources?.length || 0) > 1 && (
                      <span className="episode-source-badge">{episodeSourceLabel(episode.source, episode)}</span>
                    )}
                    {isWatched && (
                      <span className="episode-state" aria-label="已看完">
                        <CheckCircle2 size={15} strokeWidth={2.2} />
                      </span>
                    )}
                    </span>
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}
      </div>

      {visibleCast.length > 0 && (
        <section className="detail-cast-section">
          <h2>演职人员</h2>
          <div className="detail-cast-row">
            {visibleCast.map(({ person, castKey }: { person: any; castKey: string }) => {
              return (
                <article className="detail-cast-card" key={castKey}>
                  <DecodedImage
                    src={assetUrl(person.profile_path, 'poster')}
                    alt={person.name || '演职人员'}
                    onError={() => setFailedCastKeys((keys) => new Set(keys).add(castKey))}
                  />
                  <strong title={person.name}>{person.name}</strong>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {episodeContextMenu && createPortal(
        <div
          ref={episodeContextMenuRef}
          className="episode-context-menu"
          style={{ left: episodeContextMenu.x, top: episodeContextMenu.y }}
          onClick={(event) => event.stopPropagation()}
          role="menu"
          aria-label="剧集操作"
        >
          <button role="menuitem" onClick={() => { const episodeId = episodeContextMenu.episode.episode_id; setEpisodeContextMenu(null); void handlePlay(episodeId); }}>
            <Play size={15} strokeWidth={2.1} />
            <span>播放</span>
          </button>
          <button role="menuitem" onClick={() => markEpisodeCompleted(episodeContextMenu.episode, true)}>
            <CheckCircle2 size={15} strokeWidth={2.1} />
            <span>已看完</span>
          </button>
          <button role="menuitem" onClick={() => markEpisodeCompleted(episodeContextMenu.episode, false)}>
            <Circle size={15} strokeWidth={2.1} />
            <span>未看</span>
          </button>
        </div>,
        document.body,
      )}

      {episodeQuickGridOpen && createPortal(
        <div className="detail-quick-grid-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setEpisodeQuickGridOpen(false)}>
          <section className="detail-quick-grid-dialog" role="dialog" aria-modal="true" aria-label="快速选集">
            <header>
              <div><h2>快速选集</h2><span>{currentSeason?.label || '当前季度'} · 共 {episodes.length} 集</span></div>
              <button type="button" onClick={() => setEpisodeQuickGridOpen(false)} aria-label="关闭快速选集"><X size={18} /></button>
            </header>
            <div className="detail-quick-grid">
              {episodes.map((episode: any, index: number) => (
                <button
                  type="button"
                  key={episode.episode_id}
                  className={`${continueTarget?.episode_id === episode.episode_id ? 'current' : ''} ${watchedEpisodeIds.has(episode.episode_id) ? 'watched' : ''}`}
                  onClick={() => revealEpisodeInStrip(index)}
                  title={cleanDisplayTitle(episode.title || '', `第 ${episode.episode_number} 集`)}
                >
                  {String(episode.episode_number).padStart(2, '0')}
                </button>
              ))}
            </div>
          </section>
        </div>,
        document.body,
      )}

      {relatedWorks.length > 0 && (
        <section className="detail-related-section">
          <h2 className="mb-4 text-xl font-semibold" style={{ color: 'var(--text)' }}>关联作品</h2>
          <div className="detail-related-grid">
            {relatedWorks.map((related: any) => {
              const fullRelated = relatedLookup.get(related.work_id);
              const imagePath = related.fanart_path || related.poster_path || fullRelated?.fanart_path || fullRelated?.poster_path || '';
              const imageKind = related.fanart_path || fullRelated?.fanart_path ? 'backdrop' : 'poster';
              const rating = Number(related.rating ?? fullRelated?.rating ?? 0);
              const previewImage = imagePath ? assetUrl(imagePath, imageKind) : '';
              return (
              <button
                key={related.work_id}
                className="detail-related-card text-left"
                onClick={() => navigateToWorkDetail(related.work_id)}
                onMouseEnter={() => prewarmDetailNavigation(related.work_id, previewImage)}
                onFocus={() => prewarmDetailNavigation(related.work_id, previewImage)}
              >
                {imagePath ? (
                  <DecodedImage src={assetUrl(imagePath, imageKind)} alt={related.title} title={related.title} loading="lazy" />
                ) : (
                  <div className="detail-related-fallback">
                    <span title={related.title}>{related.title}</span>
                  </div>
                )}
                <div className="detail-related-copy"><strong title={related.title}>{related.title}</strong>{rating > 0 && <span><Star size={14} fill="currentColor" /> {rating.toFixed(1)}</span>}</div>
              </button>
              );
            })}
          </div>
        </section>
      )}
      {similarWorks.length > 0 && (
        <section className="detail-similar-section">
          <h2>相关推荐</h2>
          <div className="detail-similar-row">
            {similarWorks.map((item) => {
              const imagePath = item.fanart_path || item.poster_path;
              const previewImage = imagePath ? assetUrl(imagePath, item.fanart_path ? 'backdrop' : 'poster') : '';
              return <button
                key={item.work_id}
                onClick={() => navigateToWorkDetail(item.work_id)}
                onMouseEnter={() => prewarmDetailNavigation(item.work_id, previewImage)}
                onFocus={() => prewarmDetailNavigation(item.work_id, previewImage)}
                className="detail-similar-card"
              >
                {imagePath ? <DecodedImage src={previewImage} alt={item.title} loading="lazy" /> : <span className="detail-related-fallback">{item.title}</span>}
                <strong title={item.title}>{item.title}</strong>
                {Number(item.rating || 0) > 0 && <small><Star size={13} fill="currentColor" /> {Number(item.rating).toFixed(1)}</small>}
              </button>;
            })}
          </div>
        </section>
      )}
      {artworkKind && createPortal(
        <div className="seasonal-dialog-backdrop" role="presentation">
          <section className="seasonal-dialog detail-management-dialog" role="dialog" aria-modal="true" aria-label={artworkKind === 'poster' ? '更换封面' : '更换背景图'}>
            <header><div><h2>{artworkKind === 'poster' ? '更换封面' : '更换背景图'}</h2><p>手动图片优先于在线刮削，可随时恢复在线版本。</p></div><button onClick={() => { setArtworkKind(null); setArtworkFile(null); }} aria-label="关闭"><X size={18} /></button></header>
            <label className="artwork-file-field"><span>选择图片</span><input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => setArtworkFile(event.target.files?.[0] || null)} /></label>
            {artworkFile && <div className="artwork-file-summary"><Image size={18} /><span>{artworkFile.name}</span><strong>{Math.max(1, Math.round(artworkFile.size / 1024))} KB</strong></div>}
            <footer><button onClick={restoreArtwork} disabled={managementBusy}>恢复在线图片</button><button className="primary" onClick={saveArtwork} disabled={!artworkFile || managementBusy}>{managementBusy ? '保存中...' : '保存图片'}</button></footer>
          </section>
        </div>, document.body,
      )}
      {titleEditOpen && createPortal(
        <div className="seasonal-dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setTitleEditOpen(false)}>
          <section className="seasonal-dialog detail-management-dialog title-edit-dialog" role="dialog" aria-modal="true" aria-label="修改作品标题">
            <header><div><h2>修改作品标题</h2><p>手动标题会优先显示，之后重新刮削或重扫也会保留。</p></div><button onClick={() => setTitleEditOpen(false)} aria-label="关闭"><X size={18} /></button></header>
            <label><span>作品标题</span><input autoFocus maxLength={160} value={titleInput} onChange={(event) => setTitleInput(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && void saveWorkTitle()} /></label>
            <footer>
              {work.title_provenance === 'manual' && <button onClick={restoreWorkTitle} disabled={managementBusy}>恢复刮削标题</button>}
              <button onClick={() => setTitleEditOpen(false)} disabled={managementBusy}>取消</button>
              <button className="primary" onClick={saveWorkTitle} disabled={!titleInput.trim() || managementBusy}>{managementBusy ? '保存中...' : '保存标题'}</button>
            </footer>
          </section>
        </div>, document.body,
      )}
      {deleteWorkOpen && createPortal(
        <div className="seasonal-dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !managementBusy && setDeleteWorkOpen(false)}>
          <section className="seasonal-dialog detail-management-dialog delete-work-dialog" role="alertdialog" aria-modal="true" aria-label="删除该作品">
            <header>
              <div><h2>删除该作品</h2><p>此操作无法撤销。真实视频文件不会被删除。</p></div>
              <button onClick={() => setDeleteWorkOpen(false)} disabled={managementBusy} aria-label="关闭"><X size={18} /></button>
            </header>
            <strong className="delete-work-title">{work.title}</strong>
            {deletePreview && <div className="delete-work-summary">
              <span>生成文件 <b>{deletePreview.files.filter((item) => item.exists).length}</b></span>
              <span>观看历史 <b>{deletePreview.history_count}</b></span>
              <span>播放进度 <b>{deletePreview.progress_count}</b></span>
              <span>关联索引 <b>{deletePreview.related_reference_count}</b></span>
            </div>}
            {deleteError && <div className="append-dialog-status error" role="alert">{deleteError}</div>}
            <footer>
              <button onClick={() => setDeleteWorkOpen(false)} disabled={managementBusy}>取消</button>
              <button className="danger" onClick={confirmDeleteWork} disabled={managementBusy || !deletePreview || deletePreview.blocked}>
                {managementBusy ? '处理中...' : '确认删除'}
              </button>
            </footer>
          </section>
        </div>, document.body,
      )}
      {appendOpen && createPortal(
        <div className="seasonal-dialog-backdrop" role="presentation">
          <section className="seasonal-dialog detail-management-dialog append-dialog" role="dialog" aria-modal="true" aria-label="追加剧集">
            <header><div><h2>追加剧集</h2><p>只按视频文件名识别集号，父目录和作品名称不会参与判断。</p></div><button onClick={closeAppendDialog} disabled={managementBusy} aria-label="关闭"><X size={18} /></button></header>
            <label><span>目标季度</span><input type="number" min="0" value={appendSeason} onChange={(event) => { setAppendSeason(event.target.value); setAppendItems([]); setAppendPlanId(''); setAppendCanCommit(false); setAppendNotice(null); }} /></label>
            <label><span>视频文件路径</span><textarea rows={5} value={appendPaths} onChange={(event) => { setAppendPaths(event.target.value); setAppendItems([]); setAppendPlanId(''); setAppendCanCommit(false); setAppendNotice(null); }} placeholder={'H:\\新番\\作品A\\第7集.mkv\nH:\\新番\\作品A\\第8集.mkv'} /></label>
            {appendNotice && <div className={`append-dialog-status ${appendNotice.tone}`} role={appendNotice.tone === 'error' ? 'alert' : 'status'} aria-live="polite">{appendNotice.message}</div>}
            {appendItems.length > 0 && <div className="append-preview-list">{appendItems.map((item) => <div key={item.item_id} className={item.status}><strong>{item.episode_number ? `第 ${item.episode_number} 集` : '未识别'}</strong><span title={item.path}>{item.path}</span><em>{manualEpisodeStatusLabel(item.status)}</em></div>)}</div>}
            <footer><button onClick={previewAppendEpisodes} disabled={managementBusy || !appendPaths.trim()}>识别并预览</button><button className="primary" onClick={commitAppendEpisodes} disabled={managementBusy || !appendCanCommit}>{managementBusy ? '处理中...' : '确认追加'}</button></footer>
          </section>
        </div>, document.body,
      )}
      </div>
    </div>
  );
}

function CandidatePanel<T>({ title, items, getTitle, getMeta, getImage, onSelect }: {
  title: string;
  items: T[];
  getTitle: (item: T) => string;
  getMeta: (item: T) => string;
  getImage?: (item: T) => string;
  onSelect: (item: T) => void;
}) {
  return (
    <div className="candidate-panel">
      <div className="candidate-panel-head">
        <strong>{title}</strong>
        <span>人工确认</span>
      </div>
      <div className="candidate-grid">
        {items.slice(0, 8).map((item, index) => {
          const image = getImage?.(item) || '';
          const itemTitle = getTitle(item);
          const itemMeta = getMeta(item);
          return (
            <button key={index} className="candidate-card" onClick={() => onSelect(item)} title={itemTitle}>
              <div className="candidate-poster">
                {image ? <DecodedImage src={image} alt={itemTitle} title={itemTitle} loading="lazy" /> : <span>无图</span>}
              </div>
              <div className="candidate-copy">
                <strong title={itemTitle}>{itemTitle}</strong>
                <span title={itemMeta}>{itemMeta}</span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function extractBangumiSubjects(payload: any): any[] {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.data)) return payload.data;
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.results)) return payload.results;
  return [];
}

function CenteredMessage({ children }: { children: string }) {
  return (
    <div className="page-loading-wrap">
      <div className="page-loading-message">{children}</div>
    </div>
  );
}

function Tag({ children, strong = false }: { children: ReactNode; strong?: boolean }) {
  return (
    <span
      className="rounded-full px-3 py-1 text-sm"
      style={{ background: 'var(--surface-soft)', color: strong ? 'var(--accent)' : 'var(--text-muted)' }}
    >
      {children}
    </span>
  );
}

function categoryLabel(showType: string) {
  if (showType === 'anime_series') return '番剧';
  if (showType === 'anime_movie') return '动画电影';
  if (showType === 'live_series') return '剧集';
  return '电影';
}

function manualEpisodeStatusLabel(status: ManualEpisodePreviewItem['status']) {
  return ({ added: '新增', existing: '已存在', replaced: '内容已替换', conflict: '重复冲突', unrecognized: '需要确认' } as const)[status];
}

function formatEpisodeCode(episode: any) {
  const season = Math.max(0, Number(episode?.season_number || 0));
  const number = Math.max(0, Number(episode?.episode_number || 0));
  return `S${String(season).padStart(2, '0')}E${String(number).padStart(2, '0')}`;
}

function providerDisplayLabel(provider?: string): string {
  if (!provider) return '';
  return ({ pan115: '115 网盘', baidu: '百度网盘', quark: '夸克网盘', other: '其他远程来源', local: '本地' } as Record<string, string>)[provider] || provider;
}

function episodeSourceLabel(source: string, episode?: any) {
  const provider = providerDisplayLabel(episode?.provider_id);
  if (provider) return provider;
  return ({ pan115: '115', baidu: '百度', local: '本地', openlist: 'OpenList 连接' } as Record<string, string>)[source] || source;
}

function workSourceLabel(source: string, work?: any) {
  const provider = providerDisplayLabel(work?.provider_id);
  if (provider) return provider;
  return ({ pan115: '115 网盘', baidu: '百度网盘', local: '本地', openlist: 'OpenList 连接' } as Record<string, string>)[source] || source || '未知来源';
}

function normalizedWorkSources(work: any): string[] {
  const sources = [...(work.sources || []), work.source, ...(work.episodes || []).map((episode: any) => episode.source)]
    .filter(Boolean);
  return [...new Set<string>(sources)].sort((left, right) => {
    const priority = { pan115: 0, baidu: 1, local: 2, openlist: 3 } as Record<string, number>;
    return (priority[left] ?? 99) - (priority[right] ?? 99) || left.localeCompare(right);
  });
}

function isCategoryKey(value: string): value is CategoryKey {
  return value === 'anime_series' || value === 'anime_movie' || value === 'live_series' || value === 'live_movie';
}

function mergeTitleTags(work: any) {
  const tags: string[] = [];
  for (const value of [...(work.tags || []), ...(work.genres || [])]) {
    const tag = String(value || '').trim();
    if (!tag || tags.includes(tag)) continue;
    tags.push(tag);
    if (tags.length >= 6) break;
  }
  return tags;
}

function similarityScore(current: any, candidate: any) {
  const currentTags = new Set(
    [...(current.genres || []), ...(current.tags || [])]
      .map((value) => String(value || '').trim().toLocaleLowerCase())
      .filter(Boolean),
  );
  const candidateTags = new Set(
    [...(candidate.genres || []), ...(candidate.tags || [])]
      .map((value) => String(value || '').trim().toLocaleLowerCase())
      .filter(Boolean),
  );
  let score = current.show_type && current.show_type === candidate.show_type ? 2 : 0;
  for (const tag of currentTags) if (candidateTags.has(tag)) score += 3;
  return score;
}

function resolveContinueEpisode(episodes: any[], historyEpisodeId: string, watchedEpisodeIds: Set<string>) {
  if (!episodes.length) return null;
  const ordered = [...episodes].sort((a: any, b: any) => {
    const seasonDiff = Number(a.season_number || 0) - Number(b.season_number || 0);
    if (seasonDiff !== 0) return seasonDiff;
    return Number(a.episode_number || 0) - Number(b.episode_number || 0);
  });
  const historyEpisode = ordered.find((episode: any) => episode.episode_id === historyEpisodeId) || null;
  // 历史集无论是否已完成，都作为当前显示上下文：完成瞬间不得回退到全季第一集未观看，
  // 下一集只在用户实际播放或明确选择后切换。
  if (historyEpisode) return historyEpisode;
  return ordered.find((episode: any) => !watchedEpisodeIds.has(episode.episode_id)) || ordered[0];
}

function buildProgressMap(items: PlaybackProgressItem[]) {
  return new Map((items || []).map((item) => [item.episode_id, item]));
}

function progressPercent(item?: PlaybackProgressItem | null) {
  if (!item || item.completed) return 0;
  const ratio = Number(item.ratio || (item.duration > 0 ? item.position / item.duration : 0));
  if (!Number.isFinite(ratio) || ratio <= 0) return 0;
  return Math.max(1, Math.min(99, Math.round(ratio * 100)));
}

function findSelectedSeason(work: any, selectedSeasonKey: string, selectedSeasonNumber: number | null) {
  const seasons = work?.seasons || [];
  return seasons.find((season: any) => seasonOptionKey(season) === selectedSeasonKey)
    || seasons.find((season: any) => season.season_number === selectedSeasonNumber)
    || null;
}

function resolveInitialSeason(work: any, workId: string) {
  const seasons = work?.seasons || [];
  if (!seasons.length) return null;
  const remembered = useUiStore.getState().selectedSeasonByWork?.[workId];
  if (remembered?.seasonKey) {
    const byKey = seasons.find((season: any) => seasonOptionKey(season) === remembered.seasonKey);
    if (byKey) return byKey;
  }
  if (remembered?.seasonNumber !== undefined && remembered?.seasonNumber !== null) {
    const byNumber = seasons.find((season: any) => Number(season.season_number ?? 0) === remembered.seasonNumber);
    if (byNumber) return byNumber;
  }
  return seasons[0];
}

function bangumiSubjectTypes(showType: string) {
  if (showType === 'anime_series' || showType === 'anime_movie') return [2];
  if (showType === 'live_series' || showType === 'live_movie') return [6];
  return [2, 6];
}

function collectionLabel(type: number) {
  return bangumiCollectionTypes.find((item) => item.value === type)?.label || `状态 ${type}`;
}

function assetUrl(path: string, kind: 'poster' | 'backdrop' | 'detailBackdrop' | 'candidate' | 'episode' | 'logo' = 'backdrop') {
  return buildAssetUrl(path, { kind });
}

function candidateImageUrl(candidate: ScrapeCandidate) {
  const poster = candidate.poster_path || '';
  if (!poster) return '';
  return buildAssetUrl(poster, { kind: 'candidate' });
}

function bangumiSubjectImageUrl(subject: any) {
  const direct = subject?.cover || subject?.image || subject?.poster || subject?.poster_path || '';
  const images = subject?.images || {};
  const fromImages = images.small || images.grid || images.common || images.medium || images.large || '';
  const image = direct || fromImages;
  if (!image) return '';
  return buildBangumiImageUrl(String(image), 'subject');
}

function buildRelatedWorksFallback(work: any, works: any[]) {
  const currentKeys = collectRelatedTitleKeys(work);
  if (!currentKeys.size) return [];

  return (works || [])
    .filter((item) => item && item.work_id && item.work_id !== work.work_id && item.source === work.source)
    .map((item) => {
      const candidateKeys = collectRelatedTitleKeys(item);
      const sameSeries = currentKeys.has(`series:${relatedTitleKey(item.series_group)}`) || candidateKeys.has(`series:${relatedTitleKey(work.series_group)}`);
      const overlap = hasRelatedTitleOverlap(currentKeys, candidateKeys);
      if (!sameSeries && !overlap) return null;

      return {
        work_id: item.work_id,
        title: item.title || item.original_title || item.work_title || '',
        year: item.year ?? null,
        card_type: item.card_type || '',
        relation_type: item.card_type === 'standalone' ? 'movie' : (item.group_type || 'related'),
        poster_path: item.poster_path || '',
        fanart_path: item.fanart_path || '',
        show_type: item.show_type || '',
      };
    })
    .filter(Boolean)
    .slice(0, 12);
}

function collectRelatedTitleKeys(work: any) {
  const keys = new Set<string>();
  for (const value of [work?.title, work?.original_title, work?.series_group]) {
    const key = relatedTitleKey(value);
    if (key) keys.add(key);
    const stripped = relatedTitleKey(stripRelatedSuffixes(value));
    if (stripped) keys.add(stripped);
  }
  const series = relatedTitleKey(work?.series_group);
  if (series) keys.add(`series:${series}`);
  return keys;
}

function hasRelatedTitleOverlap(a: Set<string>, b: Set<string>) {
  for (const key of a) {
    if (b.has(key)) return true;
  }
  const aValues = [...a];
  const bValues = [...b];
  for (const left of aValues) {
    for (const right of bValues) {
      if (left === right) return true;
      if (left.length >= 3 && right.length >= 3 && (left.startsWith(right) || right.startsWith(left))) {
        return true;
      }
    }
  }
  return false;
}

function relatedTitleKey(value: any) {
  const text = stripRelatedSuffixes(value);
  if (!text) return '';
  return text
    .toLowerCase()
    .replace(/[·・]/g, ' ')
    .replace(/[._/\\-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function stripRelatedSuffixes(value: any) {
  return cleanDisplayTitle(
    String(value || '')
      .replace(/\s*[\[(（【][^\])）】]*[\])）】]\s*$/g, ' ')
      .replace(/\s*(?:剧场版|电影版|电影|movie|film|ova|oad|oav|特别篇|番外篇|总集篇|前篇|后篇|上篇|下篇|sp|specials?)\s*$/gi, '')
      .replace(/\s*(?:第\s*\d+\s*季|season\s*\d+|s\d{1,2})\s*$/gi, '')
      .replace(/\s+/g, ' ')
      .trim(),
    ''
  );
}

function mergeRelatedWorks(primary: any[], fallback: any[]) {
  const map = new Map<string, any>();
  for (const item of [...primary, ...fallback]) {
    if (!item?.work_id || map.has(item.work_id)) continue;
    map.set(item.work_id, item);
  }
  return [...map.values()];
}
