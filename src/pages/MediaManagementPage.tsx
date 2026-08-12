import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Spinner } from '@fluentui/react-components';
import {
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  Cloud,
  FolderOpen,
  FolderUp,
  FileUp,
  HardDrive,
  Play,
  Plus,
  RefreshCw,
  SearchCheck,
  ScanLine,
  Trash2,
  TriangleAlert,
  Wrench,
} from 'lucide-react';
import { sourcesApi } from '../api/sources';
import { configApi } from '../api/config';
import { openlistApi, type OpenListCacheMeta, type OpenListEntry, type OpenListImportBatch } from '../api/openlist';
import type { OpenListRoute, ProviderId } from '../api/types';
import { mediaPresetsApi, type MediaLibraryPreset, type PresetDeletePreview } from '../api/mediaPresets';
import { importsApi } from '../api/imports';
import { mirrorApi } from '../api/mirror';
import { scrapeApi, type ReviewQueueItem, type ScrapeCandidate } from '../api/scrape';
import { candidateDisplayTitles, formatCandidateScore } from '../utils/scrapeCandidate';
import { tasksApi } from '../api/tasks';
import type { ImportPlanItem, ImportPreview, SourcePathValidation, TaskRecord } from '../api/types';
import {
  useMediaWorkflowStore,
  type MediaWorkflowEntry as DirectoryEntry,
  type MediaWorkflowFamily,
  type MediaWorkflowSource as SourceKey,
  type MediaWorkflowStep as WorkflowStep,
} from '../stores/mediaWorkflow';
import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
import LibraryMaintenancePanel from '../components/media/LibraryMaintenancePanel';
import MediaFlowProgress from '../components/media/MediaFlowProgress';
import MediaLogList, { type MediaLog } from '../components/media/MediaLogList';
import MediaPlanSummary from '../components/media/MediaPlanSummary';
import MediaStageHeader from '../components/media/MediaStageHeader';
import MediaTaskWorkbench, { isMirrorTaskReady } from '../components/media/MediaTaskWorkbench';
import { pickFolder, pickDirectoryTreeFile } from '../platform/folderPicker';

const flowSteps: Array<Exclude<WorkflowStep, 'maintenance'>> = ['import', 'confirm', 'workbench'];

const sourceOptions: Array<{ value: SourceKey; label: string }> = [
  { value: 'local', label: '本地目录' },
  { value: 'pan115', label: '115 目录树' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'openlist', label: 'OpenList 连接' },
];

// 内容提供商标签（OpenList 导入显示真实 provider，不再统一显示为 OpenList）
const providerLabels: Record<ProviderId, string> = {
  pan115: '115 网盘',
  baidu: '百度网盘',
  quark: '夸克网盘',
  other: '其他远程来源',
  local: '本地',
};

// OpenList 选择篮中的目录项
interface OpenListSelectionItem {
  remote_path: string;
  name: string;
}

// 选择篮上限（与后端一致）
export const OPENLIST_BATCH_LIMIT = 20;

export function openlistIsAncestorOrSelf(prefix: string, path: string): boolean {
  const left = prefix.replace(/\/$/, '');
  const right = path.replace(/\/$/, '');
  if (left === '/' || left === '') return true;
  if (right === left) return true;
  return right.startsWith(left + '/');
}

export function openlistProviderLabel(provider: ProviderId | '' | undefined): string {
  if (!provider) return '其他远程来源';
  return providerLabels[provider] || '其他远程来源';
}

const familyOptions: Array<{ value: MediaWorkflowFamily; label: string }> = [
  { value: 'anime', label: '动画' },
  { value: 'live', label: '剧集' },
];

type PendingTreeAction =
  | { kind: 'create' }
  | { kind: 'update'; presetId: string; presetName: string; presetSource: 'pan115' | 'baidu'; presetSourceRoot: string };

type PendingSeasonalImport =
  | { kind: 'tree' }
  | { kind: 'folder' }
  | { kind: 'local'; entryId: string };

function makeEntry(): DirectoryEntry {
  return { id: crypto.randomUUID(), path: '', note: '', status: 'idle' };
}

function formatOpenlistSize(bytes: number | null): string {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}



export default function MediaManagementPage() {
  const step = useMediaWorkflowStore((state) => state.step);
  const source = useMediaWorkflowStore((state) => state.source);
  const family = useMediaWorkflowStore((state) => state.family);
  const importScope = useMediaWorkflowStore((state) => state.importScope);
  const entries = useMediaWorkflowStore((state) => state.entries);
  const activeEntryId = useMediaWorkflowStore((state) => state.activeEntryId);
  const task = useMediaWorkflowStore((state) => state.task);
  const taskKind = useMediaWorkflowStore((state) => state.taskKind);
  const pendingDroppedTreePath = useMediaWorkflowStore((state) => state.pendingDroppedTreePath);
  const setStep = useMediaWorkflowStore((state) => state.setStep);
  const setSource = useMediaWorkflowStore((state) => state.setSource);
  const setFamily = useMediaWorkflowStore((state) => state.setFamily);
  const setImportScope = useMediaWorkflowStore((state) => state.setImportScope);
  const setEntries = useMediaWorkflowStore((state) => state.setEntries);
  const setActiveEntryId = useMediaWorkflowStore((state) => state.setActiveEntryId);
  const setTask = useMediaWorkflowStore((state) => state.setTask);
  const setTaskKind = useMediaWorkflowStore((state) => state.setTaskKind);
  const consumeDroppedTreePath = useMediaWorkflowStore((state) => state.consumeDroppedTreePath);
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const goSettings = useUiStore((state) => state.goSettings);
  const [actionError, setActionError] = useState('');
  const [editingItem, setEditingItem] = useState<ImportPlanItem | null>(null);
  const [editDraft, setEditDraft] = useState({ work_title: '', season_number: '', episode_number: '', group_type: 'season' });
  const refreshedWorkflowTaskRef = useRef('');
  const taskStartInFlightRef = useRef(false);
  const autoAdvanceScrapeRef = useRef('');
  const autoPipelineEntryRef = useRef('');
  const refreshedPresetScrapeTaskIdsRef = useRef<Set<string>>(new Set());
  const deletePreviewAbortRef = useRef<AbortController | null>(null);
  const treeUploadRef = useRef<HTMLInputElement>(null);
  const pendingTreeActionRef = useRef<PendingTreeAction>({ kind: 'create' });
  const [presets, setPresets] = useState<MediaLibraryPreset[]>([]);
  const [presetsLoading, setPresetsLoading] = useState(true);
  const [uploadingTree, setUploadingTree] = useState(false);
  const [scanningFolder, setScanningFolder] = useState(false);
  const [importModeActive, setImportModeActive] = useState(false);
  const [savedLocalRoot, setSavedLocalRoot] = useState('');
  const [configCache, setConfigCache] = useState<{ pan115_root?: string; baidu_root?: string; openlist_server_url?: string } | null>(null);
  const [openlistConfigured, setOpenlistConfigured] = useState(false);
  const [openlistPath, setOpenlistPath] = useState('');
  const [openlistRemoteRoot, setOpenlistRemoteRoot] = useState('');
  const [openlistEntries, setOpenlistEntries] = useState<OpenListEntry[] | null>(null);
  const [openlistBrowseLoading, setOpenlistBrowseLoading] = useState(false);
  const [openlistBrowseError, setOpenlistBrowseError] = useState('');
  const [openlistScanTask, setOpenlistScanTask] = useState<TaskRecord | null>(null);
  const [openlistImporting, setOpenlistImporting] = useState(false);
  const [openlistNotice, setOpenlistNotice] = useState('');
  const openlistPathRef = useRef('');
  // OLIST-02：选择篮、缓存状态、批量导入、提供商路由
  const [openlistSelection, setOpenlistSelection] = useState<OpenListSelectionItem[]>([]);
  const [openlistCacheMeta, setOpenlistCacheMeta] = useState<OpenListCacheMeta | null>(null);
  const [openlistRoutes, setOpenlistRoutes] = useState<OpenListRoute[]>([]);
  const [openlistBatch, setOpenlistBatch] = useState<OpenListImportBatch | null>(null);
  const [openlistSelectionNotice, setOpenlistSelectionNotice] = useState('');
  const [openlistPrefetchLimit, setOpenlistPrefetchLimit] = useState(12);
  // 浏览竞争防护：只有最新请求可以提交 path/entries/cache 状态
  const openlistBrowseSeqRef = useRef(0);
  const openlistBatchIdRef = useRef('');
  // 防重复处理：轮询与恢复逻辑不能同时触发两次“进入确认计划”
  const completedOpenlistBatchIdRef = useRef('');
  const openlistPollTimerRef = useRef<number | null>(null);
  const [selectedCloudRoot, setSelectedCloudRoot] = useState('');
  const [repairingPresetId, setRepairingPresetId] = useState('');
  const [uploadMessage, setUploadMessage] = useState('');
  const [pendingSeasonalImport, setPendingSeasonalImport] = useState<PendingSeasonalImport | null>(null);
  const [deletingPreset, setDeletingPreset] = useState<MediaLibraryPreset | null>(null);
  const [deletingPresetId, setDeletingPresetId] = useState('');
  const [deletePreviewLoading, setDeletePreviewLoading] = useState(false);
  const [presetDeletePreview, setPresetDeletePreview] = useState<PresetDeletePreview | null>(null);
  const [deleteDialogError, setDeleteDialogError] = useState('');
  const [reviewPreset, setReviewPreset] = useState<MediaLibraryPreset | null>(null);
  const [scrapeReviewItems, setScrapeReviewItems] = useState<ReviewQueueItem[]>([]);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewBusyId, setReviewBusyId] = useState('');
  const [reviewQueries, setReviewQueries] = useState<Record<string, string>>({});
  const [reviewCandidates, setReviewCandidates] = useState<Record<string, ScrapeCandidate[]>>({});

  const activeEntry = entries.find((entry) => entry.id === activeEntryId) || entries[0];
  const preview = activeEntry?.preview || null;
  const reviewItems = useMemo(() => {
    if (!preview) return [];
    const deferredIssueIds = new Set(
      preview.issues.filter((issue) => issue.code === 'needs_review').flatMap((issue) => issue.item_ids),
    );
    return preview.items.filter((item) => item.resource_type === 'video' && item.action === 'generate_strm')
      .filter((item) => item.needs_review || item.confidence === 'low' || deferredIssueIds.has(item.id));
  }, [preview]);
  const blockingPreviewIssues = useMemo(
    () => preview?.issues.filter((issue) => issue.level === 'error') || [],
    [preview],
  );
  const reviewReasons = useMemo(() => {
    const reasons = new Map<string, string[]>();
    if (!preview) return reasons;
    for (const issue of preview.issues) {
      for (const itemId of issue.item_ids) {
        const current = reasons.get(itemId) || [];
        if (!current.includes(issue.message)) current.push(issue.message);
        reasons.set(itemId, current);
      }
    }
    return reasons;
  }, [preview]);




  const stepIndex = flowSteps.findIndex((item) => item === step);
  const taskResult = (task?.result || {}) as Record<string, unknown>;
  const taskLogs = Array.isArray(taskResult.logs) ? taskResult.logs as MediaLog[] : [];
  const activeTask = task?.status === 'pending' || task?.status === 'running';
  const indexedPresets = useMemo(() => presets.filter((preset) => preset.is_library_indexed), [presets]);
  const pendingPresets = useMemo(() => presets.filter((preset) => !preset.is_library_indexed), [presets]);

  const loadPresets = async (silent = false) => {
    if (!silent) setPresetsLoading(true);
    try {
      const result = await mediaPresetsApi.list();
      setPresets(result.presets || []);
      setActionError('');
    } catch (error) {
      setActionError(`媒体库预设读取失败：${(error as Error).message}`);
    } finally {
      if (!silent) setPresetsLoading(false);
    }
  };

  useEffect(() => { void loadPresets(); }, []);

  useEffect(() => () => deletePreviewAbortRef.current?.abort(), []);

  useEffect(() => {
    let active = true;
    configApi.getConfig()
      .then((config) => {
        const localRoot = config.local_root.trim();
        const openlistUrl = config.openlist_server_url.trim();
        if (active) {
          setSavedLocalRoot(localRoot);
          setConfigCache({
            pan115_root: config.pan115_root.trim(),
            baidu_root: config.baidu_root.trim(),
            openlist_server_url: openlistUrl,
          });
          setOpenlistConfigured(Boolean(openlistUrl && config.openlist_mount_root.trim() && config.openlist_configured));
          setOpenlistPrefetchLimit(config.openlist_prefetch_limit ?? 12);
          if (openlistUrl && config.openlist_mount_root.trim() && config.openlist_configured) {
            void openlistApi.getRoutes().then((routesResult) => setOpenlistRoutes(routesResult.routes)).catch(() => undefined);
          }
        }
      })
      .catch(() => {
        // 配置读取失败不阻断导入，用户仍可通过目录选择器填写路径。
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (source !== 'local' || !savedLocalRoot) return;
    setEntries((items) => {
      if (!items.length) {
        const entry = makeEntry();
        return [{ ...entry, path: savedLocalRoot }];
      }
      let changed = false;
      const next = items.map((item, index) => {
        if (index !== 0 || item.path.trim() || item.status !== 'idle') return item;
        changed = true;
        return { ...item, path: savedLocalRoot };
      });
      return changed ? next : items;
    });
  }, [source, savedLocalRoot, setEntries]);

  const hasQueuedScrape = presets.some((preset) => preset.scrape_task?.status === 'pending' || preset.scrape_task?.status === 'running');
  useEffect(() => {
    if (!hasQueuedScrape) return;
    const timer = window.setInterval(() => {
      void loadPresets(true);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [hasQueuedScrape]);

  useEffect(() => {
    if (!task || !activeTask) return;
    const timer = window.setInterval(() => {
      void tasksApi.get(task.task_id).then(setTask).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [task?.task_id, activeTask]);

  useEffect(() => {
    if (step !== 'confirm' || !activeEntry?.planId || !activeEntry.preview || activeTask) return;
    if (activeEntry.pathValidation?.ok === false) return;
    if (activeEntry.preview.issues.some((issue) => issue.level === 'error')) return;
    const pipelineKey = `${source}:${activeEntry.planId}`;
    if (autoPipelineEntryRef.current === pipelineKey) return;
    autoPipelineEntryRef.current = pipelineKey;
    const entryId = activeEntry.id;
    const planId = activeEntry.planId;
    const previewStatus = activeEntry.preview.status;
    void (async () => {
      try {
        setActionError('');
        let durableTaskId = '';
        if (previewStatus === 'draft') {
          const confirmed = await importsApi.confirm(source, planId);
          if (confirmed.execution_mode === 'durable' && confirmed.job_id) {
            // V3：镜像 job 已由后端确认事务入队，前端无需再生成
            durableTaskId = confirmed.job_id;
          }
        }
        const confirmedPreview = await importsApi.getPreview(source, planId);
        updateEntry(entryId, { preview: confirmedPreview, status: 'parsed' });
        setTask(null);
        setTaskKind('mirror');
        setStep('workbench');
        if (durableTaskId) {
          setTask(await tasksApi.get(durableTaskId));
        } else {
          const created = await mirrorApi.generate(source, planId);
          setTask(await tasksApi.get(created.task_id));
        }
      } catch (error) {
        autoPipelineEntryRef.current = '';
        setActionError(`自动处理未能继续：${(error as Error).message}。你仍可在此页处理识别结果后重试。`);
      }
    })();
  }, [activeEntry?.id, activeEntry?.pathValidation?.ok, activeEntry?.planId, activeEntry?.preview, activeTask, setStep, source, step]);

  useEffect(() => {
    if (taskKind !== 'mirror' || !isMirrorTaskReady(task) || !task || !activeEntry?.planId) return;
    if (autoAdvanceScrapeRef.current === task.task_id) return;
    autoAdvanceScrapeRef.current = task.task_id;
    void startTask('scrape');
  }, [task, taskKind, activeEntry?.planId]);

  useEffect(() => {
    if (!task || task.status !== 'succeeded') return;
    if (taskKind !== 'mirror' && taskKind !== 'scrape') return;
    if (refreshedWorkflowTaskRef.current === task.task_id) return;
    refreshedWorkflowTaskRef.current = task.task_id;
    void Promise.all([loadLibrary({ force: true }), loadPresets()]).catch((error: Error) => {
      setActionError(`${taskKind === 'mirror' ? '镜像生成' : '刮削'}完成，但页面刷新失败：${error.message}`);
    });
  }, [loadLibrary, setStep, task?.status, task?.task_id, taskKind]);

  useEffect(() => {
    const completed = presets
      .map((preset) => preset.scrape_task)
      .filter((item): item is TaskRecord => Boolean(item && item.status === 'succeeded'))
      .filter((item) => !refreshedPresetScrapeTaskIdsRef.current.has(item.task_id));
    if (!completed.length) return;
    for (const item of completed) refreshedPresetScrapeTaskIdsRef.current.add(item.task_id);
    void loadLibrary({ force: true }).catch((error: Error) => {
      setActionError(`刮削完成，但作品列表刷新失败：${error.message}`);
    });
  }, [loadLibrary, presets]);

  const updateEntry = (id: string, patch: Partial<DirectoryEntry>) => {
    setEntries((items) => items.map((item) => item.id === id ? { ...item, ...patch } : item));
  };

  const removeDirectoryEntry = (entryId: string) => {
    const remainingEntries = entries.filter((entry) => entry.id !== entryId);
    setEntries(remainingEntries);
    if (activeEntryId === entryId) {
      setActiveEntryId(remainingEntries[0]?.id || '');
    }
  };

  const chooseLocalFolder = async (entry: DirectoryEntry) => {
    setActiveEntryId(entry.id);
    try {
      const selected = await pickFolder(entry.path);
      if (selected) updateEntry(entry.id, { path: selected, status: 'idle', error: '' });
    } catch (error) {
      updateEntry(entry.id, { error: `无法打开目录选择器：${(error as Error).message}` });
    }
  };

  const chooseTreeFile = async (action: PendingTreeAction) => {
    pendingTreeActionRef.current = action;
    // 原生 TXT 选择器：新建默认打开对应来源挂载根；更新默认打开目标预设自身的 source_root。
    const defaultPath = (() => {
      if (action.kind === 'update') return action.presetSourceRoot;
      return source === 'pan115' ? configCache?.pan115_root : configCache?.baidu_root || '';
    })();
    try {
      const selected = await pickDirectoryTreeFile(
        defaultPath || undefined,
        action.kind === 'update' ? '选择新版目录树 TXT' : '选择目录树 TXT',
      );
      if (selected) await importTreePath(selected, action);
    } catch (error) {
      setActionError(`无法打开目录树选择器：${(error as Error).message}`);
    }
  };

  const importTreePath = async (treePath: string, action: PendingTreeAction) => {
    // 创建导入要求页面全局来源不是本地目录；更新导入不依赖全局 source，
    // 必须按目标预设自身的 source（action.presetSource）执行，
    // 这样默认处于“本地目录”状态时仍可更新已有的 115/百度预设。
    if (action.kind === 'create' && source === 'local') return;
    setUploadingTree(true);
    setActionError('');
    setUploadMessage(action.kind === 'update'
      ? `正在更新“${action.presetName}”：比较新旧目录树并生成增量计划…`
      : '正在保存目录树并创建可持续更新的媒体库…');
    try {
      const result = action.kind === 'update'
        ? await mediaPresetsApi.updateFromPath(action.presetId, treePath, action.presetSource)
        : await mediaPresetsApi.createFromPath(treePath, {
          source: source as 'pan115' | 'baidu',
          importFamily: family,
          importScope: family === 'anime' ? importScope : '',
        });
      const entry = makeEntry();
      entry.path = result.version.original_name;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(result.preset.source);
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      setUploadMessage(result.diff
        ? `比较完成：新增 ${result.diff.added_count}，缺失 ${result.diff.missing_count}，未变化 ${result.diff.unchanged_count}`
        : result.reused_preset && result.unchanged
          ? `${getPresetDisplayName(result.preset)} 已存在，内容相同，未创建重复卡片或版本`
          : `${result.preset.name} 已创建，目录树已由 KumiPlayer 保存`);
      if (action.kind === 'create') setSelectedCloudRoot('');
      await loadPresets();
      setStep('confirm');
    } catch (error) {
      const message = (error as Error).message;
      setActionError(action.kind === 'update'
        ? `更新“${action.presetName}”失败：${message}${message === '媒体库预设不存在' ? '。列表可能已过期，请刷新页面后重试。' : ''}`
        : `创建媒体库失败：${message}`);
      setUploadMessage('');
    } finally {
      setUploadingTree(false);
      pendingTreeActionRef.current = { kind: 'create' };
    }
  };

  const chooseCloudContentRoot = async (initialPath = '', title = '选择实际视频文件夹') => {
    try {
      const selected = await pickFolder(initialPath, title);
      if (selected) setSelectedCloudRoot(selected);
      return selected;
    } catch (error) {
      setActionError(`无法打开目录选择器：${(error as Error).message}`);
      return null;
    }
  };

  const importTreeFile = async (file: File) => {
    const action = pendingTreeActionRef.current;
    // 与 importTreePath 一致：创建导入仅在全局 source=local 时阻止，
    // 更新导入按目标预设 source 执行，不依赖页面全局来源。
    if (action.kind === 'create' && source === 'local') return;
    setUploadingTree(true);
    setActionError('');
    setUploadMessage(action.kind === 'update'
      ? `正在更新“${action.presetName}”：比较新旧目录树并生成增量计划…`
      : '正在保存目录树并创建可持续更新的媒体库…');
    try {
      const result = action.kind === 'update'
        ? await mediaPresetsApi.update(action.presetId, file)
        : await mediaPresetsApi.create(file, {
          source: source as 'pan115' | 'baidu',
          sourceRoot: selectedCloudRoot,
          importFamily: family,
          importScope: family === 'anime' ? importScope : '',
        });
      const entry = makeEntry();
      entry.path = result.version.original_name;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(result.preset.source);
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      setUploadMessage(result.diff
        ? `比较完成：新增 ${result.diff.added_count}，缺失 ${result.diff.missing_count}，未变化 ${result.diff.unchanged_count}`
        : result.reused_preset && result.unchanged
          ? `${getPresetDisplayName(result.preset)} 已存在，内容相同，未创建重复卡片或版本`
        : `${result.preset.name} 已创建，目录树已由 KumiPlayer 保存`);
      if (action.kind === 'create') setSelectedCloudRoot('');
      await loadPresets();
      setStep('confirm');
    } catch (error) {
      const message = (error as Error).message;
      setActionError(action.kind === 'update'
        ? `更新“${action.presetName}”失败：${message}${message === '媒体库预设不存在' ? '。列表可能已过期，请刷新页面后重试。' : ''}`
        : `创建媒体库失败：${message}`);
      setUploadMessage('');
    } finally {
      setUploadingTree(false);
      pendingTreeActionRef.current = { kind: 'create' };
      if (treeUploadRef.current) treeUploadRef.current.value = '';
    }
  };

  const importDroppedTreePath = async (treePath: string) => {
    setImportModeActive(true);
    setStep('import');
    setUploadingTree(true);
    setActionError('');
    setUploadMessage('正在识别 TXT 来源并创建可持续更新的媒体库…');
    try {
      const result = await mediaPresetsApi.createFromPath(treePath, {
        importFamily: family,
        importScope: '',
      });
      const entry = makeEntry();
      entry.path = result.version.original_name;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(result.preset.source);
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      setUploadMessage(
        result.reused_preset && result.unchanged
          ? `已识别为${result.preset.source === 'pan115' ? ' 115' : '百度网盘'}目录树，并复用现有${getPresetDisplayName(result.preset)}`
          : `已识别为${result.preset.source === 'pan115' ? ' 115' : '百度网盘'}目录树，${result.preset.name} 已创建`,
      );
      await loadPresets();
      setStep('confirm');
    } catch (error) {
      setActionError(`拖放导入失败：${(error as Error).message}`);
      setUploadMessage('');
    } finally {
      setUploadingTree(false);
    }
  };

  useEffect(() => {
    if (!pendingDroppedTreePath) return;
    const treePath = consumeDroppedTreePath();
    if (treePath) void importDroppedTreePath(treePath);
  }, [pendingDroppedTreePath]);

  const scanSeasonalFolder = async () => {
    const root = selectedCloudRoot.trim();
    if (!root) {
      setActionError('请先选择新番真实文件夹。');
      return;
    }
    setScanningFolder(true);
    setActionError('');
    setUploadMessage('正在读取文件夹名称和剧集信息…');
    try {
      const result = await mediaPresetsApi.scanFolder({
        source: 'baidu',
        sourceRoot: root,
        importFamily: 'anime',
        importScope: 'seasonal',
      });
      const entry = makeEntry();
      entry.path = result.version.original_name;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(result.preset.source);
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      setUploadMessage(`扫描完成：识别 ${result.preset.work_count} 部作品、${result.preset.video_count} 个视频`);
      await loadPresets();
      setStep('confirm');
    } catch (error) {
      setActionError(`扫描新番文件夹失败：${(error as Error).message}`);
      setUploadMessage('');
    } finally {
      setScanningFolder(false);
    }
  };

  const browseOpenlist = async (path: string, refresh = false) => {
    const seq = ++openlistBrowseSeqRef.current;
    setOpenlistBrowseLoading(true);
    setOpenlistBrowseError('');
    try {
      const result = await openlistApi.browse(path, 1, refresh);
      if (seq !== openlistBrowseSeqRef.current) return; // 过期响应丢弃：只提交最新请求
      setOpenlistPath(result.path);
      setOpenlistRemoteRoot(result.remote_root);
      openlistPathRef.current = result.path;
      setOpenlistEntries(result.entries);
      setOpenlistCacheMeta(result.cache || null);
      // 有界预取：只预取当前层少量直接子目录（后端单并发、generation 可取消、不递归、失败静默）
      const childDirs = result.entries.filter((entry) => entry.is_dir).slice(0, Math.max(0, openlistPrefetchLimit));
      if (childDirs.length) {
        void openlistApi.prefetch(childDirs.map((entry) => entry.remote_path)).catch(() => undefined);
      }
    } catch (error) {
      if (seq !== openlistBrowseSeqRef.current) return;
      setOpenlistBrowseError((error as Error).message);
    } finally {
      if (seq === openlistBrowseSeqRef.current) setOpenlistBrowseLoading(false);
    }
  };

  // —— 选择篮：跨层保留、父子去重（后端再次校验） ——
  const providerForPath = (remotePath: string): ProviderId => {
    let best: OpenListRoute | null = null;
    for (const route of openlistRoutes) {
      if (!route.enabled) continue;
      if (!openlistIsAncestorOrSelf(route.remote_prefix, remotePath)) continue;
      if (!best || route.remote_prefix.length > best.remote_prefix.length) best = route;
    }
    return best?.provider_id ?? 'other';
  };

  const toggleOpenlistSelection = (entry: OpenListEntry) => {
    if (!entry.is_dir) return;
    setOpenlistSelectionNotice('');
    setOpenlistSelection((current) => {
      if (current.some((item) => item.remote_path === entry.remote_path)) {
        return current.filter((item) => item.remote_path !== entry.remote_path);
      }
      if (current.length >= OPENLIST_BATCH_LIMIT) {
        setOpenlistSelectionNotice(`一次最多选择 ${OPENLIST_BATCH_LIMIT} 个目录`);
        return current;
      }
      // 已选祖先存在时阻止选择后代
      const hasAncestor = current.some((item) => openlistIsAncestorOrSelf(item.remote_path, entry.remote_path) && item.remote_path !== entry.remote_path);
      if (hasAncestor) {
        setOpenlistSelectionNotice('该目录已被已选的父目录包含，请勿重复选择');
        return current;
      }
      // 选择父目录时自动移除已选后代
      const withoutDescendants = current.filter(
        (item) => !openlistIsAncestorOrSelf(entry.remote_path, item.remote_path),
      );
      return [...withoutDescendants, { remote_path: entry.remote_path, name: entry.name }];
    });
  };

  const removeOpenlistSelection = (remotePath: string) => {
    setOpenlistSelection((current) => current.filter((item) => item.remote_path !== remotePath));
    setOpenlistSelectionNotice('');
  };

  const clearOpenlistSelection = () => {
    setOpenlistSelection([]);
    setOpenlistSelectionNotice('');
  };

  const isOpenlistSelected = (remotePath: string) =>
    openlistSelection.some((item) => item.remote_path === remotePath);

  useEffect(() => {
    if (source !== 'openlist' || !openlistConfigured) return;
    if (openlistPathRef.current) return; // 已在浏览中
    void browseOpenlist('');
    return () => {
      // 离开/切换来源：使旧浏览请求与旧预取 generation 失效
      openlistBrowseSeqRef.current += 1;
      void openlistApi.prefetch([]).catch(() => undefined);
    };
  }, [source, openlistConfigured]);

  const openlistScanActive = openlistScanTask?.status === 'pending' || openlistScanTask?.status === 'running';

  const stopOpenlistPolling = () => {
    if (openlistPollTimerRef.current !== null) {
      window.clearInterval(openlistPollTimerRef.current);
      openlistPollTimerRef.current = null;
    }
  };

  useEffect(() => () => stopOpenlistPolling(), []);

  // —— V2 durable batch：createImportBatch → 轮询 getImportBatch → plan_ids 进确认页 ——
  const summarizeOpenlistBatch = (batch: OpenListImportBatch) => {
    const succeeded = batch.roots.filter((root) => root.job_status === 'succeeded' || root.status === 'succeeded').length;
    const failed = batch.roots.filter((root) => root.job_status === 'failed' || root.status === 'failed').length;
    const cancelled = batch.roots.filter((root) => root.job_status === 'cancelled' || root.status === 'cancelled').length;
    const active = batch.roots.find((root) => ['pending', 'queued', 'running'].includes(root.job_status || root.status));
    setOpenlistNotice(
      active
        ? `正在扫描 ${active.remote_locator}`
        : `批次${batch.status === 'succeeded' ? '完成' : '已停止'}：${succeeded} 个成功、${failed} 个失败、${cancelled} 个取消`,
    );
  };

  // 批次终态恢复：succeeded root 的 plan_ids（可能多个 revision）逐个生成确认条目进入确认页
  const collectOpenlistBatchEntries = async (batch: OpenListImportBatch) => {
    const succeeded = batch.roots.filter((root) => root.job_status === 'succeeded' || root.status === 'succeeded');
    const failed = batch.roots.filter((root) => root.job_status === 'failed' || root.status === 'failed');
    const cancelled = batch.roots.filter((root) => root.job_status === 'cancelled' || root.status === 'cancelled');
    const failureText = [
      failed.length ? `${failed.length} 个失败` : '',
      cancelled.length ? `${cancelled.length} 个取消` : '',
    ].filter(Boolean).join('、');
    if (!succeeded.length) {
      setOpenlistNotice(`批量导入未产生可确认目录${failureText ? `：${failureText}` : '，任务已停止'}`);
      return;
    }
    setOpenlistNotice(failureText
      ? `批量扫描完成：${succeeded.length} 个成功、${failureText}（失败目录可在选择篮中重试）`
      : `批量扫描完成：${succeeded.length} 个目录扫描成功`);
    const newEntries: DirectoryEntry[] = [];
    const previewFailures: string[] = [];
    for (const root of succeeded) {
      const planIds = root.plan_ids || [];
      if (!planIds.length) {
        previewFailures.push(root.remote_locator);
        continue;
      }
      for (const planId of planIds) {
        try {
          const preview = await importsApi.getPreview('openlist', planId);
          const entry = makeEntry();
          entry.path = root.remote_locator;
          entry.note = 'OpenList 目录';
          entry.status = 'parsed';
          entry.planId = planId;
          entry.preview = preview;
          newEntries.push(entry);
        } catch {
          // 单个预览失败：记录并可重试，不静默丢弃
          previewFailures.push(root.remote_locator);
          break;
        }
      }
    }
    if (previewFailures.length) {
      setActionError(`部分成功目录的预览加载失败（可在媒体库预设中重新打开）：${previewFailures.join('、')}`);
    }
    if (newEntries.length) {
      setEntries(newEntries);
      setActiveEntryId(newEntries[0].id);
      setImportModeActive(false);
      await loadPresets();
      setStep('confirm');
    }
  };

  // 终态处理（带防重复保护）：批次终态只从 SQLite batch/root/job 状态恢复确认条目
  const handleOpenlistBatchFinished = async (batch: OpenListImportBatch) => {
    if (completedOpenlistBatchIdRef.current === batch.batch_id) return;
    completedOpenlistBatchIdRef.current = batch.batch_id;
    summarizeOpenlistBatch(batch);
    await collectOpenlistBatchEntries(batch);
  };

  // 接管批次（创建或恢复共用）：记录 batch_id、开启轮询、镜像 active discovery job 进度
  const attachOpenlistBatch = (batch: OpenListImportBatch, reason = '') => {
    stopOpenlistPolling();
    setOpenlistBatch(batch);
    openlistBatchIdRef.current = batch.batch_id;
    const activeRoot = batch.roots.find((root) => ['pending', 'queued', 'running'].includes(root.job_status || root.status));
    if (activeRoot?.job_id) {
      void tasksApi.get(activeRoot.job_id).then(setOpenlistScanTask).catch(() => undefined);
    }
    setOpenlistImporting(Boolean(activeRoot));
    if (reason) setOpenlistNotice(reason);
    openlistPollTimerRef.current = window.setInterval(() => {
      void openlistApi.getImportBatch(batch.batch_id).then((updated) => {
        setOpenlistBatch(updated);
        const current = updated.roots.find((root) => ['pending', 'queued', 'running'].includes(root.job_status || root.status));
        if (current?.job_id) void tasksApi.get(current.job_id).then(setOpenlistScanTask).catch(() => undefined);
        const active = Boolean(current);
        setOpenlistImporting(active);
        if (!active) {
          stopOpenlistPolling();
          void handleOpenlistBatchFinished(updated);
        }
      }).catch(() => undefined);
    }, 1200);
  };

  const recoverOpenlistTask = async () => {
    try {
      const payload = await tasksApi.list({ source: 'openlist', task_type: 'discovery_scan', limit: 100 });
      const active = (payload.tasks || []).find((task) => task.status === 'pending' || task.status === 'running');
      if (active) {
        setOpenlistScanTask(active);
        setOpenlistImporting(true);
        setOpenlistNotice('已恢复后台目录扫描');
      }
    } catch {
      // 恢复查询失败不阻断目录浏览
    }
  };

  useEffect(() => {
    if (source !== 'openlist' || !openlistConfigured) return;
    if (openlistScanTask || openlistBatch) return; // 本次会话已接管批次/任务
    void recoverOpenlistTask();
  }, [source, openlistConfigured]);

  useEffect(() => {
    // OpenList 首发不支持自动追更：切到 OpenList 来源时重置新番范围
    if (source === 'openlist' && importScope === 'seasonal') setImportScope('');
  }, [source, importScope]);

  const startOpenlistImport = async () => {
    const path = openlistPathRef.current;
    if (!path || openlistImporting) return;
    setActionError('');
    setOpenlistImporting(true);
    setOpenlistNotice('');
    try {
      const batch = await openlistApi.createImportBatch({
        remote_paths: [path],
        import_family: family,
        import_scope: family === 'anime' ? importScope : '',
      });
      setOpenlistScanTask(null);
      attachOpenlistBatch(batch, `已创建持久批次，正在扫描 ${batch.roots.length} 个目录…`);
    } catch (error) {
      setActionError(`OpenList 导入失败：${(error as Error).message}`);
      setOpenlistImporting(false);
    }
  };

  // 批量导入：选择篮中的目录创建 durable batch，每个 root 独立 discovery job
  const startOpenlistBatchImport = async () => {
    if (!openlistSelection.length || openlistImporting) return;
    if (openlistSelection.length > OPENLIST_BATCH_LIMIT) {
      setOpenlistSelectionNotice(`一次最多导入 ${OPENLIST_BATCH_LIMIT} 个目录`);
      return;
    }
    setActionError('');
    setOpenlistSelectionNotice('');
    setOpenlistImporting(true);
    setOpenlistNotice('');
    try {
      const batch = await openlistApi.createImportBatch({
        remote_paths: openlistSelection.map((item) => item.remote_path),
        import_family: family,
        import_scope: family === 'anime' ? importScope : '',
      });
      setOpenlistScanTask(null);
      attachOpenlistBatch(batch, `已创建持久批次，正在扫描 ${batch.roots.length} 个目录…`);
    } catch (error) {
      setActionError(`批量导入失败：${(error as Error).message}`);
      setOpenlistImporting(false);
    }
  };

  const refreshOpenlistTask = async () => {
    if (openlistBatchIdRef.current) {
      try {
        const updated = await openlistApi.getImportBatch(openlistBatchIdRef.current);
        attachOpenlistBatch(updated);
      } catch (error) {
        setActionError(`OpenList 批次状态刷新失败：${(error as Error).message}`);
      }
      return;
    }
    const taskId = openlistScanTask?.task_id;
    if (!taskId) return;
    try {
      setOpenlistScanTask(await tasksApi.get(taskId));
    } catch (error) {
      setActionError(`OpenList 任务状态刷新失败：${(error as Error).message}`);
    }
  };

  const cancelOpenlistScan = () => {
    if (openlistBatch?.batch_id) {
      void openlistApi.cancelImportBatch(openlistBatch.batch_id)
        .then((updated) => attachOpenlistBatch(updated))
        .catch((error) => setActionError(`OpenList 批次取消失败：${(error as Error).message}`));
      return;
    }
    const taskId = openlistScanTask?.task_id;
    if (taskId) void tasksApi.cancel(taskId).then(setOpenlistScanTask).catch(() => undefined);
  };

  const rescanOpenlistPreset = async (preset: MediaLibraryPreset) => {
    setActionError('');
    setOpenlistNotice('');
    try {
      const result = await openlistApi.rescanPreset(preset.preset_id);
      const initial = await tasksApi.get(result.task_id);
      setOpenlistScanTask(initial);
      setOpenlistImporting(true);
      setOpenlistNotice(`正在更新 OpenList 媒体库「${preset.name}」…`);
      const taskId = initial.task_id;
      stopOpenlistPolling();
      openlistPollTimerRef.current = window.setInterval(() => {
        void tasksApi.get(taskId).then((updated) => {
          setOpenlistScanTask(updated);
          if (updated.status === 'pending' || updated.status === 'running') return;
          setOpenlistImporting(false);
          stopOpenlistPolling();
          setOpenlistNotice(updated.status === 'succeeded'
            ? `OpenList 媒体库「${preset.name}」更新完成`
            : `OpenList 媒体库更新失败：${updated.error || updated.message || '请重试'}`);
        }).catch(() => undefined);
      }, 1200);
    } catch (error) {
      setActionError(`OpenList 更新失败：${(error as Error).message}`);
    }
  };

  type OpenlistScanProgress = {
    phase?: string;
    overall_total_known?: boolean;
    current_path?: string;
    current_page?: number;
    current_directory_total?: number | null;
    current_directory_collected?: number;
    scanned_directory_count?: number;
    queued_directory_count?: number;
    found_directory_count?: number;
    found_file_count?: number;
    found_entry_count?: number;
    found_video_candidate_count?: number;
    video_count?: number;
    work_count?: number;
    batch_index?: number;
    batch_total?: number;
    current_remote_path?: string;
    batch_summary?: { succeeded?: number; failed?: number };
  };
  const openlistScanResult = (openlistScanTask?.result || {}) as OpenlistScanProgress;
  const openlistCacheStatusText = (() => {
    if (!openlistCacheMeta) return '';
    if (openlistCacheMeta.refreshing) return '缓存数据 · 正在后台更新'; if (openlistCacheMeta.status === 'fresh') return '来自本地缓存'; if (openlistCacheMeta.status === 'stale') return openlistCacheMeta.refresh_failed ? '缓存数据 · 刷新失败，已保留旧缓存' : '缓存数据 · 即将后台更新'; return '';
  })();
  const openlistCrumbs = useMemo(() => {
    const root = openlistRemoteRoot || '/';
    const rootLabel = root === '/' ? '根目录' : root.split('/').filter(Boolean).pop() || root;
    const parts = openlistPath.split('/').filter(Boolean);
    const rootParts = root === '/' ? [] : root.split('/').filter(Boolean);
    const crumbs: Array<{ label: string; path: string }> = [{ label: rootLabel, path: root }];
    let current = root;
    for (const part of parts.slice(rootParts.length)) {
      current = current === '/' ? `/${part}` : `${current}/${part}`;
      crumbs.push({ label: part, path: current });
    }
    return crumbs;
  }, [openlistPath, openlistRemoteRoot]);

  const openlistParentPath = useMemo(() => {
    const root = openlistRemoteRoot || '/';
    if (!openlistPath || openlistPath === root) return null;
    const parts = openlistPath.split('/').filter(Boolean);
    parts.pop();
    return parts.length ? `/${parts.join('/')}` : root;
  }, [openlistPath, openlistRemoteRoot]);

  const parseEntry = async (entry: DirectoryEntry) => {
    const path = entry.path.trim();
    if (!path) {
      updateEntry(entry.id, { status: 'failed', error: '请先填写目录或目录树文件路径。' });
      return;
    }
    setActionError('');
    updateEntry(entry.id, { status: 'parsing', error: '' });
    try {
      const result = source === 'local'
        ? await sourcesApi.scanLocal(path, family, family === 'anime' ? importScope : '')
        : await sourcesApi.parse(
          source,
          path,
          undefined,
          family,
          family === 'anime' ? importScope : '',
        );
      const parsedPreview = await importsApi.getPreview(source, result.plan_id);
      const pathValidation: SourcePathValidation | undefined = 'path_validation' in result
        ? result.path_validation as SourcePathValidation | undefined
        : undefined;
      updateEntry(entry.id, {
        status: 'parsed',
        planId: result.plan_id,
        preview: parsedPreview,
        resolvedRoot: pathValidation?.resolved_root,
        pathValidation,
        error: '',
      });
      setActiveEntryId(entry.id);
      if (source === 'local') await loadPresets();
      setStep('confirm');
    } catch (error) {
      updateEntry(entry.id, { status: 'failed', error: (error as Error).message });
    }
  };

  const requestTreeImport = () => {
    if (family === 'anime' && importScope === 'seasonal') {
      setPendingSeasonalImport({ kind: 'tree' });
      return;
    }
    chooseTreeFile({ kind: 'create' });
  };

  const requestFolderScan = () => {
    if (!selectedCloudRoot.trim()) {
      setActionError('请先选择新番真实文件夹。');
      void chooseCloudContentRoot('', '选择新番真实文件夹');
      return;
    }
    setPendingSeasonalImport({ kind: 'folder' });
  };

  const requestEntryParse = (entry: DirectoryEntry) => {
    if (family === 'anime' && importScope === 'seasonal') {
      setPendingSeasonalImport({ kind: 'local', entryId: entry.id });
      return;
    }
    void parseEntry(entry);
  };

  const confirmSeasonalImport = () => {
    const pending = pendingSeasonalImport;
    setPendingSeasonalImport(null);
    if (!pending) return;
    if (pending.kind === 'folder') {
      void scanSeasonalFolder();
      return;
    }
    if (pending.kind === 'tree') {
      chooseTreeFile({ kind: 'create' });
      return;
    }
    const entry = entries.find((item) => item.id === pending.entryId);
    if (entry) void parseEntry(entry);
  };

  const resumePreset = async (preset: MediaLibraryPreset) => {
    setActionError('');
    setUploadMessage('');
    let workingPreset = preset;
    let workingVersion = preset.versions[preset.versions.length - 1];
    let parsedPreview: ImportPreview | null = null;
    try {
      if (workingPreset.update_mode === 'directory_tree' && workingVersion?.path_validation?.ok === false) {
        setRepairingPresetId(preset.preset_id);
        setUploadMessage(`正在重新验证“${getPresetDisplayName(preset)}”的实际视频路径…`);
        const refreshed = await mediaPresetsApi.revalidate(preset.preset_id);
        workingPreset = refreshed.preset;
        workingVersion = refreshed.version;
        parsedPreview = refreshed.preview;
        await loadPresets();
        if (workingVersion.path_validation?.ok === false) {
          setActionError(`“${getPresetDisplayName(workingPreset)}”当前保存的实际视频根目录仍未通过验证，请选择实际文件夹并重新验证。`);
          setUploadMessage('');
          return;
        }
        setUploadMessage('实际视频路径已重新验证，可以继续处理导入计划。');
      }
      if (!parsedPreview) {
        parsedPreview = await importsApi.getPreview(workingPreset.source, workingPreset.current_plan_id);
      }
      const entry = makeEntry();
      entry.path = workingVersion?.original_name || '已保存目录树';
      entry.note = workingPreset.name;
      entry.presetId = workingPreset.preset_id;
      entry.status = 'parsed';
      entry.planId = workingPreset.current_plan_id;
      entry.preview = parsedPreview;
      entry.resolvedRoot = workingPreset.source_root;
      entry.pathValidation = workingVersion?.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(workingPreset.source);
      setFamily(workingPreset.import_family);
      setImportScope(workingPreset.import_scope);
      setStep(workingPreset.lifecycle_status === 'draft'
        ? 'confirm'
        : 'workbench');
    } catch (error) {
      setActionError(`无法继续处理“${getPresetDisplayName(preset)}”：${(error as Error).message}`);
      setUploadMessage('');
    } finally {
      setRepairingPresetId('');
    }
  };

  const rescanLocalPreset = async (preset: MediaLibraryPreset) => {
    if (scanningFolder) return;
    setScanningFolder(true);
    setActionError('');
    setUploadMessage(`正在重新扫描“${preset.source_root}”并计算增量变化…`);
    try {
      const result = await mediaPresetsApi.rescanLocal(preset.preset_id);
      const entry = makeEntry();
      entry.path = result.preset.source_root;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource('local');
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      setUploadMessage(result.unchanged
        ? '扫描完成，本地文件没有变化。'
        : `增量扫描完成：新增 ${result.diff?.added_count || 0}，缺失 ${result.diff?.missing_count || 0}，未变化 ${result.diff?.unchanged_count || 0}`);
      await loadPresets();
      setStep('confirm');
    } catch (error) {
      setActionError(`重新扫描本地目录失败：${(error as Error).message}`);
      setUploadMessage('');
    } finally {
      setScanningFolder(false);
    }
  };

  const rebindPresetRoot = async (preset: MediaLibraryPreset) => {
    if (repairingPresetId) return;
    const selected = await chooseCloudContentRoot(
      preset.source_root,
      preset.import_scope === 'seasonal' ? '选择新番真实文件夹' : '选择实际视频文件夹',
    );
    if (!selected) return;
    setRepairingPresetId(preset.preset_id);
    setActionError('');
    try {
      const result = await mediaPresetsApi.rebindRoot(preset.preset_id, selected);
      const entry = makeEntry();
      entry.path = result.version.original_name;
      entry.note = result.preset.name;
      entry.presetId = result.preset.preset_id;
      entry.status = 'parsed';
      entry.planId = result.preview.plan_id;
      entry.preview = result.preview;
      entry.resolvedRoot = result.preset.source_root;
      entry.pathValidation = result.version.path_validation;
      setEntries([entry]);
      setActiveEntryId(entry.id);
      setSource(result.preset.source);
      setFamily(result.preset.import_family);
      setImportScope(result.preset.import_scope);
      await loadPresets();
      setUploadMessage(result.version.path_validation?.ok
        ? '实际视频目录已重新验证，可以继续确认导入计划。'
        : '所选目录仍未通过抽样验证，请检查是否选择了目录树内容的精确根目录。');
      setStep('confirm');
    } catch (error) {
      setActionError(`重新验证实际视频目录失败：${(error as Error).message}`);
    } finally {
      setRepairingPresetId('');
    }
  };

  const queuePresetScrape = async (preset: MediaLibraryPreset) => {
    if (!preset.current_plan_id) return;
    setActionError('');
    try {
      const created = await scrapeApi.autoScrape(preset.source, preset.current_plan_id);
      const queuedTask = await tasksApi.get(created.task_id);
      setTaskKind('scrape');
      setTask(queuedTask);
      await loadPresets(true);
      setUploadMessage(`“${getPresetDisplayName(preset)}”已加入刮削队列，完成后会自动开始下一项。`);
    } catch (error) {
      setActionError(`“${getPresetDisplayName(preset)}”加入刮削队列失败：${(error as Error).message}`);
    }
  };

  const openScrapeReview = async (preset: MediaLibraryPreset) => {
    setReviewPreset(preset);
    setReviewLoading(true);
    setActionError('');
    try {
      const payload = await scrapeApi.getReviewQueue(preset.source);
      const items = (payload.items || []).filter((item) => item.import_plan_id === preset.current_plan_id);
      setScrapeReviewItems(items);
      setReviewQueries(Object.fromEntries(items.map((item) => [item.scrape_target_id, item.scrape_title || item.local_title])));
      setReviewCandidates(Object.fromEntries(items.map((item) => [item.scrape_target_id, item.candidates || []])));
    } catch (error) {
      setActionError(`人工匹配列表读取失败：${(error as Error).message}`);
      setReviewPreset(null);
    } finally {
      setReviewLoading(false);
    }
  };

  const searchReviewCandidates = async (item: ReviewQueueItem) => {
    setReviewBusyId(item.scrape_target_id);
    try {
      const candidates = await scrapeApi.getCandidates(
        item.scrape_target_id,
        reviewQueries[item.scrape_target_id] || item.scrape_title,
        item.scrape_year || undefined,
      );
      setReviewCandidates((current) => ({ ...current, [item.scrape_target_id]: candidates }));
    } catch (error) {
      setActionError(`重新搜索候选作品失败：${(error as Error).message}`);
    } finally {
      setReviewBusyId('');
    }
  };

  const selectReviewCandidate = async (item: ReviewQueueItem, candidate: ScrapeCandidate) => {
    setReviewBusyId(item.scrape_target_id);
    try {
      const created = await scrapeApi.selectCandidate(
        item.scrape_target_id,
        candidate.tmdb_id,
        candidate.tmdb_type,
        'manual',
        reviewQueries[item.scrape_target_id] || item.scrape_title,
        item.local_season_number || undefined,
      );
      setScrapeReviewItems((items) => items.filter((entry) => entry.scrape_target_id !== item.scrape_target_id));
      setTaskKind('scrape');
      setTask(await tasksApi.get(created.task_id));
      await loadPresets(true);
    } catch (error) {
      setActionError(`提交人工匹配失败：${(error as Error).message}`);
    } finally {
      setReviewBusyId('');
    }
  };

  const skipReviewItem = async (item: ReviewQueueItem) => {
    setReviewBusyId(item.scrape_target_id);
    try {
      await scrapeApi.skipReviewItem(item.scrape_target_id);
      setScrapeReviewItems((items) => items.filter((entry) => entry.scrape_target_id !== item.scrape_target_id));
      await loadPresets(true);
    } catch (error) {
      setActionError(`跳过人工匹配项失败：${(error as Error).message}`);
    } finally {
      setReviewBusyId('');
    }
  };

  const openPresetDelete = (preset: MediaLibraryPreset) => {
    setDeletingPreset(preset);
    setPresetDeletePreview(null);
    setDeleteDialogError('');
  };

  const closePresetDelete = () => {
    if (deletingPresetId) return;
    deletePreviewAbortRef.current?.abort();
    deletePreviewAbortRef.current = null;
    setDeletePreviewLoading(false);
    setDeletingPreset(null);
    setPresetDeletePreview(null);
    setDeleteDialogError('');
  };

  const preparePresetDelete = async () => {
    if (!deletingPreset || deletePreviewLoading || deletingPresetId) return;
    const controller = new AbortController();
    deletePreviewAbortRef.current = controller;
    setDeletePreviewLoading(true);
    setActionError('');
    setDeleteDialogError('');
    try {
      const nextPreview = await mediaPresetsApi.deletePreview(deletingPreset.preset_id, controller.signal);
      if (!controller.signal.aborted) setPresetDeletePreview(nextPreview);
    } catch (error) {
      if (!controller.signal.aborted) {
        setDeleteDialogError(`生成删除预览失败：${(error as Error).message}`);
      }
    } finally {
      if (deletePreviewAbortRef.current === controller) {
        deletePreviewAbortRef.current = null;
        setDeletePreviewLoading(false);
      }
    }
  };

  const removePreset = async () => {
    if (!deletingPreset || !presetDeletePreview || deletingPresetId) return;
    const preset = deletingPreset;
    setDeletingPresetId(preset.preset_id);
    setActionError('');
    setDeleteDialogError('');
    try {
      const result = await mediaPresetsApi.deleteConfirm(preset.preset_id, presetDeletePreview.preview_id);
      if (!result.deleted_preset) {
        const detail = result.failed.map((item) => item.reason).filter(Boolean).slice(0, 2).join('；');
        setDeleteDialogError(`卡片尚未删除${detail ? `：${detail}` : ''}。请排除占用后重试。`);
        await loadPresets(true);
        return;
      }
      setPresets((items) => items.filter((item) => item.preset_id !== preset.preset_id));
      setDeletingPreset(null);
      setPresetDeletePreview(null);
      setUploadMessage(
        `已删除导入卡片“${getPresetDisplayName(preset)}”`
        + `${result.deleted_archive_count ? `、${result.deleted_archive_count} 个目录树版本` : ''}`
        + '；镜像、刮削结果和媒体库数据均已保留。',
      );
    } catch (error) {
      setDeleteDialogError(`删除失败：${(error as Error).message}`);
    } finally {
      setDeletingPresetId('');
    }
  };

  const renderPresetCards = (items: MediaLibraryPreset[]) => (
    <div className="media-preset-grid">{items.map((preset) => {
      const latestVersion = preset.versions[preset.versions.length - 1];
      const title = getPresetDisplayName(preset);
      const ordinalLabel = getPresetOrdinalLabel(preset);
      const scrapeTask = preset.scrape_task || null;
      const scrapeResult = (scrapeTask?.result || {}) as Record<string, unknown>;
      const scrapeProgress = Math.max(0, Math.min(100, Number(scrapeTask?.progress || 0)));
      const queuePosition = Number(scrapeResult.queue_position || 0);
      const scrapeActive = scrapeTask?.status === 'pending' || scrapeTask?.status === 'running';
      const scrapeStopped = scrapeTask?.status === 'cancelled'
        || (scrapeTask?.status === 'failed' && scrapeTask?.message === '已停止');
      const scrapeFailed = !scrapeStopped && (
        scrapeTask?.status === 'failed'
        || Boolean(String(scrapeResult.error || '').trim())
        || Number(scrapeResult.failed || 0) > 0
      );
      const scrapeFailReason = String(scrapeTask?.error || scrapeResult.error || '').trim()
        || (Number(scrapeResult.failed || 0) > 0 ? '部分刮削目标处理失败，请重新刮削' : '');
      const hasManualReview = preset.lifecycle_status === 'needs_attention' && (preset.review_count || 0) > 0;
      const pathValidation = latestVersion?.path_validation;
      const taskLabel = scrapeTask?.status === 'pending'
        ? queuePosition > 1 ? `排队等待 · 前方 ${queuePosition - 1} 项` : '排队等待 · 即将开始'
        : scrapeTask?.status === 'running' ? `正在刮削 · ${Math.round(scrapeProgress)}%`
          : scrapeStopped ? `上次刮削已停止 · ${Math.round(scrapeProgress)}%`
          : scrapeFailed ? `上次刮削失败${scrapeFailReason ? ` · ${scrapeFailReason}` : ''}`
          : hasManualReview ? `需处理 ${preset.review_count} 个刮削匹配` : '';
      return (
        <article className="media-preset-card" key={preset.preset_id}>
          <div className="media-preset-card-top">
            <div><span className={`media-preset-source ${preset.source}`}>{preset.source === 'local' ? '本地目录' : preset.source === 'pan115' ? '115 网盘' : preset.source === 'openlist' ? openlistProviderLabel(preset.provider_id) : '百度网盘'}</span><span>{preset.import_scope === 'seasonal' ? '新番追更' : '已完结'}</span>{ordinalLabel && <span className="media-preset-ordinal">{ordinalLabel}</span>}</div>
            <div className="media-preset-card-tools">
              {scrapeTask && <div className={`media-preset-progress-ring ${scrapeTask.status}`} style={{ background: `conic-gradient(var(--accent-strong) ${scrapeProgress * 3.6}deg, color-mix(in srgb, var(--border) 72%, transparent) 0deg)` }} aria-label={`刮削进度 ${Math.round(scrapeProgress)}%`}><span>{scrapeTask.status === 'pending' ? queuePosition || '·' : Math.round(scrapeProgress)}</span></div>}
              <Button appearance="subtle" icon={<Trash2 size={15} />} aria-label={`维护或删除${title}`} title="维护或删除媒体库" disabled={Boolean(deletingPresetId) || scrapeActive} onClick={() => openPresetDelete(preset)} />
            </div>
          </div>
          <div className="media-preset-card-title"><strong>{title}</strong><small>{latestVersion?.original_name || (preset.update_mode === 'local_scan' ? '本地路径扫描' : '目录树文件')}</small></div>
          <div className={`media-preset-path ${pathValidation?.ok ? 'verified' : pathValidation ? 'invalid' : 'unknown'}`}>
            <span>{pathValidation?.ok ? <CheckCircle2 size={14} /> : pathValidation ? <TriangleAlert size={14} /> : <SearchCheck size={14} />}{pathValidation?.ok ? '路径已验证' : pathValidation ? '路径待修复' : '生成时验证路径'}</span>
            <strong title={preset.source_root || '未配置'}>{preset.source_root || '未配置实际视频根目录'}</strong>
          </div>
          <span className={`media-preset-lifecycle ${preset.lifecycle_status}`}>{getPresetLifecycleLabel(preset.lifecycle_status)}</span>
          {taskLabel && <div className={`media-preset-task-state ${scrapeActive ? 'active' : scrapeStopped ? 'stopped' : 'attention'}`}>{scrapeActive && <Spinner size="tiny" />}<span>{taskLabel}</span></div>}
          <div className="media-preset-stats"><span><strong>{preset.work_count}</strong> 识别作品</span><span><strong>{preset.video_count}</strong> 个视频</span><span><strong>{preset.version_count}</strong> {preset.update_mode === 'local_scan' ? '次扫描' : '个版本'}</span></div>
          <small>上次更新：{new Date(preset.updated_at).toLocaleString()}</small>
          <div className="media-preset-card-actions">
            {preset.update_mode === 'directory_tree' && pathValidation?.ok === false && <Button className="media-preset-action repair" appearance="secondary" icon={<FolderOpen size={15} />} disabled={Boolean(repairingPresetId) || uploadingTree} onClick={() => void rebindPresetRoot(preset)}>选择实际文件夹并重新验证</Button>}
            {hasManualReview && <Button className="media-preset-action primary" appearance="primary" disabled={uploadingTree} onClick={() => void openScrapeReview(preset)}>处理刮削匹配</Button>}
            {!scrapeActive && scrapeFailed && !hasManualReview && <Button className="media-preset-action primary" appearance="primary" disabled={uploadingTree} onClick={() => void queuePresetScrape(preset)}>重新刮削</Button>}
            {scrapeActive && <Button className="media-preset-action primary" appearance="primary" disabled={uploadingTree} onClick={() => { setTaskKind('scrape'); setTask(scrapeTask); setStep('workbench'); }}>查看进度</Button>}
            {!scrapeActive && !scrapeFailed && preset.lifecycle_status === 'mirrored' && <Button className="media-preset-action primary" appearance="primary" disabled={uploadingTree} onClick={() => void queuePresetScrape(preset)}>{scrapeStopped ? '继续刮削' : '加入刮削队列'}</Button>}
            {!scrapeActive && !preset.is_library_indexed && preset.lifecycle_status !== 'mirrored' && preset.lifecycle_status !== 'needs_attention' && <Button className="media-preset-action primary" appearance="primary" disabled={uploadingTree || repairingPresetId === preset.preset_id} onClick={() => void resumePreset(preset)}>{pathValidation?.ok === false ? '重新验证并继续' : '继续处理'}</Button>}
            {preset.update_mode === 'local_scan'
              ? <Button className="media-preset-action secondary" appearance="secondary" icon={scanningFolder ? <Spinner size="tiny" /> : <ScanLine size={15} />} disabled={scanningFolder || uploadingTree} onClick={() => void rescanLocalPreset(preset)}>重新扫描本地目录</Button>
              : preset.update_mode === 'directory_tree' && <Button className="media-preset-action secondary" appearance="secondary" icon={<FileUp size={15} />} disabled={uploadingTree} onClick={() => chooseTreeFile({ kind: 'update', presetId: preset.preset_id, presetName: title, presetSource: preset.source as 'pan115' | 'baidu', presetSourceRoot: preset.source_root })}>导入新版并安全比对</Button>}
          </div>
        </article>
      );
    })}</div>
  );

  const confirmPlan = async () => {
    if (!activeEntry?.planId || !preview || activeEntry.pathValidation?.ok === false) return;
    if (preview.status !== 'draft' && preview.status !== 'confirmed') return;
    setActionError('');
    try {
      // 已确认计划不重复提交确认：仅 draft 调用确认 API，confirmed 直接刷新预览并进入工作台。
      if (preview.status === 'draft') {
        const confirmed = await importsApi.confirm(source, activeEntry.planId);
        if (confirmed.execution_mode === 'durable' && confirmed.job_id) {
          // V3：后端已入队 durable mirror job，工作台直接挂接该任务，不再重复生成
          setTaskKind('mirror');
          setTask(await tasksApi.get(confirmed.job_id));
        }
      }
      const confirmedPreview = await importsApi.getPreview(source, activeEntry.planId);
      updateEntry(activeEntry.id, { preview: confirmedPreview });
      setStep('workbench');
    } catch (error) {
      setActionError((error as Error).message);
    }
  };

  const saveItem = async () => {
    if (!editingItem || !activeEntry?.planId) return;
    const patch: Record<string, unknown> = {
      work_title: editDraft.work_title.trim(),
      group_type: editDraft.group_type,
      needs_review: false,
      warnings: [],
    };
    if (editDraft.season_number.trim()) patch.season_number = Number(editDraft.season_number);
    if (editDraft.episode_number.trim()) patch.episode_number = Number(editDraft.episode_number);
    try {
      await importsApi.patchItem(source, editingItem.id, activeEntry.planId, patch);
      const refreshedPreview = await importsApi.getPreview(source, activeEntry.planId);
      updateEntry(activeEntry.id, { preview: refreshedPreview });
      setEditingItem(null);
    } catch (error) {
      setActionError((error as Error).message);
    }
  };

  const startTask = async (kind: 'mirror' | 'scrape') => {
    if (!activeEntry?.planId || activeTask || taskStartInFlightRef.current) return;
    if (kind === 'mirror' && activeEntry.pathValidation?.ok === false) {
      setActionError(`路径验证失败：${activeEntry.pathValidation.message || '实际视频文件不可达，请修正挂载路径后重新导入目录树。'}`);
      return;
    }
    setActionError('');
    taskStartInFlightRef.current = true;
    try {
      const result = kind === 'mirror'
        ? await mirrorApi.generate(source, activeEntry.planId)
        : await scrapeApi.autoScrape(source, activeEntry.planId);
      setTaskKind(kind);
      setTask(await tasksApi.get(result.task_id));
    } catch (error) {
      setActionError((error as Error).message);
    } finally {
      taskStartInFlightRef.current = false;
    }
  };

  const cancelTask = async () => {
    if (!task) return;
    try {
      setTask(await tasksApi.cancel(task.task_id));
    } catch (error) {
      setActionError((error as Error).message);
    }
  };

  const handleWorkbenchStart = () => {
    if (taskKind === 'scrape' && isScrapeTask(task)) {
      void startTask('scrape');
      return;
    }
    if (taskKind === 'mirror' && isMirrorTaskReady(task)) {
      void startTask('scrape');
      return;
    }
    void startTask('mirror');
  };

  const beginNewImport = () => {
    const entry = makeEntry();
    setEntries([entry]);
    setActiveEntryId(entry.id);
    setTask(null);
    setTaskKind(null);
    setPendingSeasonalImport(null);
    setSelectedCloudRoot('');
    setUploadMessage('');
    setActionError('');
    setImportModeActive(true);
    setStep('import');
  };

  const canEnterStep = (target: WorkflowStep) => {
    if (target === 'maintenance') return true;
    const targetIndex = flowSteps.findIndex((item) => item === target);
    if (targetIndex === 0) return true;
    if (target === 'workbench' && isScrapeTask(task)) return true;
    if (!activeEntry?.planId) return false;
    if (targetIndex === 1) return true;
    if (preview?.status !== 'confirmed' && preview?.status !== 'executed') return false;
    return true;
  };

  const openEditor = (item: ImportPlanItem) => {
    setEditingItem(item);
    setEditDraft({
      work_title: item.work_title || item.series_group || '',
      season_number: item.season_number == null ? '' : String(item.season_number),
      episode_number: item.episode_number == null ? '' : String(item.episode_number),
      group_type: item.group_type || 'season',
    });
  };

  return (
    <div className="media-flow-page">
      <header className="media-flow-header">
        <div className="media-flow-command-bar">
          <div className="media-flow-title">
            <span>媒体管理</span>
            <h1>{step === 'maintenance' ? '媒体库维护' : '导入媒体'}</h1>
          </div>
          <MediaFlowProgress
            step={step}
            completedThrough={stepIndex + (task?.status === 'succeeded' ? 1 : 0)}
            canEnter={canEnterStep}
            onStepChange={(target) => { if (target === 'import') setImportModeActive(false); setStep(target); }}
          />
          <div className="media-flow-utilities">
            <Button
              className={`maintenance-nav-command ${step === 'maintenance' ? 'active' : ''}`}
              appearance="subtle"
              icon={<Wrench size={16} />}
              aria-current={step === 'maintenance' ? 'page' : undefined}
              onClick={() => setStep('maintenance')}
            >
              媒体库维护
            </Button>
          </div>
        </div>
      </header>

      {actionError && <div className="media-flow-alert error"><TriangleAlert size={16} />{actionError}</div>}

      {step === 'import' && (
        <section className="media-stage-shell media-source-stage">
          <MediaStageHeader
            icon={<FolderOpen size={21} />}
            eyebrow="第 1 步"
            title={importModeActive ? '选择来源' : '媒体库'}
            description={importModeActive ? '告诉 KumiPlayer 媒体在哪里，其余信息可以保持默认。' : '继续未完成的导入，或添加新的媒体目录。'}
            action={importModeActive
              ? <Button appearance="subtle" onClick={() => { setImportModeActive(false); setActionError(''); }}>返回媒体库</Button>
              : <Button className="media-primary-command" appearance="primary" icon={<Plus size={16} />} onClick={() => { setImportModeActive(true); setActionError(''); }}>添加媒体目录</Button>}
          />
            <input ref={treeUploadRef} className="media-tree-file-input" type="file" accept=".txt,.tree,.log,text/plain" onChange={(event) => { const file = event.target.files?.[0]; if (file) void importTreeFile(file); }} />
            {importModeActive && (<>
            <section className={`media-import-setup${family === 'anime' && importScope === 'seasonal' ? ' seasonal-risk' : ''}`} aria-label={source === 'local' ? '本地媒体导入设置' : '首次导入目录树'}>
              <div className="media-source-choice-grid" aria-label="媒体来源">
                {sourceOptions.map((option) => {
                  const SourceIcon = option.value === 'local' ? HardDrive : Cloud;
                  return (
                    <button
                      type="button"
                      key={option.value}
                      className={`media-source-choice${source === option.value ? ' selected' : ''}`}
                      aria-pressed={source === option.value}
                      onClick={() => setSource(option.value)}
                    >
                      <SourceIcon size={20} />
                      <span><strong>{option.label}</strong><small>{option.value === 'local' ? '电脑硬盘或局域网目录' : option.value === 'pan115' ? '导入已导出的 115 目录树' : option.value === 'baidu' ? '导入已导出的百度网盘目录树' : '浏览 OpenList 远端目录并选择多个目录批量导入'}</small></span>
                      {source === option.value && <CheckCircle2 size={17} />}
                    </button>
                  );
                })}
              </div>
              <div className="media-source-controls">
                <label><span>媒体分类</span><select value={family} onChange={(event) => setFamily(event.target.value as MediaWorkflowFamily)}>{familyOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
                {family === 'anime' && <label className={importScope === 'seasonal' ? 'seasonal-risk-field' : ''}><span>作品状态</span><select value={importScope} onChange={(event) => setImportScope(event.target.value as '' | 'seasonal')}><option value="">已完结（推荐）</option><option value="seasonal" disabled={source === 'openlist'}>新番（追更中）{source === 'openlist' ? '（暂不支持）' : ''}</option></select></label>}
              </div>
              {family === 'anime' && importScope === 'seasonal' && source !== 'openlist' && (
                <div className="media-import-scope-warning" role="alert">
                  <TriangleAlert size={19} />
                  <div><strong>当前选择的是新番（追更中）</strong><span>仅用于仍在更新的作品。导入前会再次确认，避免把整批已完结动画误放进新番库。</span></div>
                </div>
              )}
              {source === 'openlist' && (
                <div className="media-import-scope-warning" role="note">
                  <TriangleAlert size={19} />
                  <div><strong>首发暂不支持 OpenList 自动追更</strong><span>请使用「已完结」导入 OpenList 媒体；新番追更会在单独验证后开放。</span></div>
                </div>
              )}
              {source === 'baidu' && (
                <div className="media-import-methods">
                  <div className="media-import-primary-action">
                    <div><strong>目录树 TXT 批量导入</strong><span>先在百度网盘官网导出目录树 TXT，再从挂载盘选择该文件；TXT 所在文件夹将作为实际媒体根目录。</span></div>
                    <div className="media-import-command-row">
                      <a className="media-import-outbound-link" href="https://pan.baidu.com/" target="_blank" rel="noreferrer">打开百度网盘官网 <ChevronRight size={14} /></a>
                      <Button appearance="primary" aria-label="选择目录树 TXT 并导入" icon={uploadingTree ? <Spinner size="tiny" /> : <FileUp size={16} />} disabled={uploadingTree || scanningFolder} onClick={requestTreeImport}>选择目录树 TXT</Button>
                    </div>
                  </div>
                  {family === 'anime' && importScope === 'seasonal' && (
                    <div className="media-import-primary-action secondary-method">
                      <div><strong>扫描新番真实文件夹</strong><span>直接建立首个版本；只读取名称、大小和修改时间。</span></div>
                      <Button appearance="secondary" icon={scanningFolder ? <Spinner size="tiny" /> : <ScanLine size={16} />} disabled={uploadingTree || scanningFolder || !selectedCloudRoot.trim()} onClick={requestFolderScan}>扫描并导入</Button>
                    </div>
                  )}
                </div>
              )}

              {source === 'pan115' && (
                <div className="media-import-methods">
                  <div className="media-import-primary-action">
                    <div><strong>目录树 TXT 批量导入</strong><span>先在 115 官网生成目录树 TXT，再从挂载盘选择该文件；TXT 所在文件夹将作为实际媒体根目录。</span></div>
                    <div className="media-import-command-row">
                      <a className="media-import-outbound-link" href="https://115.com" target="_blank" rel="noreferrer">前往 115 官网生成目录树 <ChevronRight size={14} /></a>
                      <Button appearance="primary" aria-label="选择目录树 TXT 并导入" icon={uploadingTree ? <Spinner size="tiny" /> : <FileUp size={16} />} disabled={uploadingTree || scanningFolder} onClick={requestTreeImport}>选择目录树 TXT</Button>
                    </div>
                  </div>
                </div>
              )}

              {source === 'openlist' && (
                <div className="media-openlist-browser" aria-label="OpenList 目录浏览器">
                  {!openlistConfigured ? (
                    <div className="media-openlist-unconfigured">
                      <TriangleAlert size={19} />
                      <div><strong>尚未配置 OpenList 连接</strong><span>请先到设置页填写 OpenList 服务地址、账号、远端映射根与本地挂载根路径。</span></div>
                      <Button appearance="primary" onClick={() => goSettings()}>前往设置</Button>
                    </div>
                  ) : openlistScanActive ? (
                    <div className="media-openlist-scan-progress" role="status">
                      <div className="media-openlist-scan-head">
                        <Spinner size="medium" />
                        <div>
                          <strong>{openlistPhaseLabel(openlistScanResult.phase, openlistScanTask?.message)}</strong>
                          <span>{openlistNotice || '正在读取远端目录树，总量仍在统计中'}</span>
                        </div>
                        <Button appearance="secondary" onClick={cancelOpenlistScan}>取消</Button>
                      </div>
                      {openlistScanResult.current_path && (
                        <dl className="media-openlist-scan-facts">
                          <div><dt>当前目录</dt><dd title={openlistScanResult.current_path}>{openlistScanResult.current_path}</dd></div>
                          <div><dt>已扫描</dt><dd>{openlistScanResult.scanned_directory_count ?? 0} 个目录</dd></div>
                          <div><dt>待扫描</dt><dd>{openlistScanResult.queued_directory_count ?? 0} 个目录</dd></div>
                          <div><dt>已发现</dt><dd>{openlistScanResult.found_entry_count ?? 0} 个条目，其中 {openlistScanResult.found_file_count ?? 0} 个文件、{openlistScanResult.found_video_candidate_count ?? 0} 个视频候选</dd></div>
                          {openlistScanResult.phase === 'remote_scan' && (
                            <div><dt>当前目录读取</dt><dd>{openlistScanResult.current_directory_total != null
                              ? `${openlistScanResult.current_directory_collected ?? 0} / ${openlistScanResult.current_directory_total} 项`
                              : `正在读取第 ${openlistScanResult.current_page ?? 1} 页`}</dd></div>
                          )}
                        </dl>
                      )}
                      <small className="media-openlist-scan-total-note">整个目录树总量仍在统计中{openlistScanResult.found_video_candidate_count != null ? '，作品数将在扫描完成后识别' : ''}</small>
                    </div>
                  ) : (
                    <>
                      {openlistScanResult.phase === 'openlist_batch' && (
                        <dl className="media-openlist-scan-facts">
                          <div><dt>批量进度</dt><dd>第 {openlistScanResult.batch_index != null ? openlistScanResult.batch_index + 1 : '?'} / {openlistScanResult.batch_total ?? '?'} 个目录</dd></div>
                          <div><dt>当前目录</dt><dd title={openlistScanResult.current_remote_path || openlistScanResult.current_path}>{openlistScanResult.current_remote_path || openlistScanResult.current_path || '—'}</dd></div>
                          {(openlistScanResult.batch_summary?.succeeded != null || openlistScanResult.batch_summary?.failed != null) && (
                            <div><dt>已汇总</dt><dd>{openlistScanResult.batch_summary?.succeeded ?? 0} 成功 / {openlistScanResult.batch_summary?.failed ?? 0} 失败</dd></div>
                          )}
                        </dl>
                      )}
                      {openlistBatch && !openlistImporting && (
                        <div className={`media-openlist-scan-terminal ${openlistBatch.status === 'succeeded' ? 'success' : 'failed'}`} role="status">
                          {openlistBatch.status === 'succeeded' ? <CheckCircle2 size={18} /> : <TriangleAlert size={18} />}
                          <div><strong>持久批次{openlistBatch.status === 'succeeded' ? '完成' : '已停止'}</strong><span>{openlistNotice || '批次已停止，可在选择篮中重新导入失败目录。'}</span></div>
                        </div>
                      )}
                      {openlistScanTask?.status === 'failed' && (
                        <div className="media-openlist-scan-terminal failed" role="alert">
                          <TriangleAlert size={18} />
                          <div><strong>OpenList 扫描失败</strong><span>{openlistScanTask.error || openlistScanTask.message || '请重试'}</span></div>
                          <div className="media-openlist-scan-terminal-actions">
                            <Button appearance="secondary" size="small" icon={<RefreshCw size={14} />} disabled={openlistImporting} onClick={() => void startOpenlistImport()}>重试</Button>
                            <Button appearance="subtle" size="small" icon={<RefreshCw size={14} />} disabled={openlistImporting} onClick={() => void refreshOpenlistTask()}>刷新任务状态</Button>
                          </div>
                        </div>
                      )}
                      {openlistScanTask?.status === 'cancelled' && (
                        <div className="media-openlist-scan-terminal stopped" role="status">
                          <TriangleAlert size={18} />
                          <div><strong>扫描已停止</strong><span>已保留原有媒体库，不会生成半成品。</span></div>
                          <div className="media-openlist-scan-terminal-actions">
                            <Button appearance="secondary" size="small" icon={<ScanLine size={14} />} disabled={openlistImporting} onClick={() => void startOpenlistImport()}>重新扫描</Button>
                          </div>
                        </div>
                      )}
                      {openlistNotice && <div className="media-openlist-notice" role="status"><CheckCircle2 size={15} />{openlistNotice}</div>}
                      <div className="media-openlist-toolbar">
                        <nav className="media-openlist-crumbs" aria-label="远端目录面包屑">
                          {openlistCrumbs.map((crumb, index) => (
                            <span key={crumb.path}>
                              {index > 0 && <ChevronRight size={13} />}
                              {index < openlistCrumbs.length - 1
                                ? <button type="button" onClick={() => void browseOpenlist(crumb.path)}>{crumb.label}</button>
                                : <strong>{crumb.label}</strong>}
                            </span>
                          ))}
                        </nav>
                        <Button appearance="secondary" icon={<FolderUp size={15} />} disabled={!openlistParentPath} onClick={() => openlistParentPath && void browseOpenlist(openlistParentPath)}>上级</Button>
                        <Button appearance="secondary" icon={<RefreshCw size={15} />} disabled={openlistBrowseLoading} onClick={() => void browseOpenlist(openlistPathRef.current, true)} title="只刷新当前层，不递归刷新后代">强制刷新当前层</Button>
                      </div>
                      {openlistCacheStatusText && <div className="media-openlist-cache-status" role="status">{openlistCacheStatusText}</div>}
                      {openlistBrowseError && (
                        <div className="media-flow-alert error" role="alert">
                          <TriangleAlert size={16} />{openlistBrowseError}
                          <Button appearance="secondary" size="small" icon={<RefreshCw size={14} />} onClick={() => void browseOpenlist(openlistPathRef.current)}>重试</Button>
                        </div>
                      )}
                      {openlistBrowseLoading && <div className="media-openlist-loading"><Spinner size="small" />正在读取目录…</div>}
                      {!openlistBrowseLoading && openlistEntries !== null && (
                        <>
                          <div className="media-openlist-entries" role="list" aria-label="远端目录条目">
                            {openlistEntries.map((entry) => entry.is_dir ? (
                              <div className="media-openlist-entry-row" role="listitem" key={entry.remote_path}>
                                <label className="media-openlist-select" title="加入选择篮（跨层保留，父子目录不重复）">
                                  <input type="checkbox" checked={isOpenlistSelected(entry.remote_path)} onChange={() => toggleOpenlistSelection(entry)} aria-label={`选择 ${entry.name}`} />
                                </label>
                                <button type="button" className="media-openlist-entry dir" onClick={() => void browseOpenlist(entry.remote_path)}>
                                  <FolderOpen size={16} /><span>{entry.name}</span><ChevronRight size={14} />
                                </button>
                              </div>
                            ) : (
                              <div className="media-openlist-entry file" role="listitem" key={entry.remote_path}>
                                <span className="media-openlist-file-icon" />
                                <span>{entry.name}</span>
                                <small>{entry.size != null ? formatOpenlistSize(entry.size) : ''}</small>
                              </div>
                            ))}
                          </div>
                          {openlistEntries.length === 0 && <div className="media-directory-empty">此目录为空。</div>}
                          <div className="media-openlist-foot">
                            <span>{openlistPath} · 共 {openlistEntries.length} 项</span>
                            <Button appearance="secondary" icon={<ScanLine size={15} />} disabled={openlistImporting} onClick={() => void startOpenlistImport()}>导入当前文件夹</Button>
                            <Button appearance="primary" icon={openlistImporting ? <Spinner size="tiny" /> : <FolderOpen size={15} />} disabled={!openlistSelection.length || openlistImporting} onClick={() => void startOpenlistBatchImport()}>批量导入 {openlistSelection.length} 个目录</Button>
                          </div>
                          {openlistSelection.length > 0 && (
                            <div className="media-openlist-basket">
                              <div className="media-openlist-basket-head">
                                <strong>选择篮（{openlistSelection.length}/{OPENLIST_BATCH_LIMIT}）</strong>
                                <Button appearance="subtle" size="small" onClick={clearOpenlistSelection}>清空</Button>
                              </div>
                              <ul className="media-openlist-basket-list">
                                {openlistSelection.map((item) => (
                                  <li key={item.remote_path}>
                                    <span className="media-openlist-basket-provider">{openlistProviderLabel(providerForPath(item.remote_path))}</span>
                                    <strong>{item.name}</strong>
                                    <code title={item.remote_path}>{item.remote_path}</code>
                                    <Button appearance="subtle" size="small" onClick={() => removeOpenlistSelection(item.remote_path)}>移除</Button>
                                  </li>
                                ))}
                              </ul>
                              {openlistSelectionNotice && <p className="media-openlist-basket-notice" role="status">{openlistSelectionNotice}</p>}
                            </div>
                          )}
                        </>
                      )}
                    </>
                  )}
                </div>
              )}
            </section>
            {source === 'local' && (
            <div className="media-directory-table" role="table" aria-label="目录树条目">
              <div className="media-directory-row head" role="row"><span>#</span><span>目录路径</span><span>备注</span><span>状态</span><span>操作</span></div>
              {entries.map((entry, index) => (
                <div className="media-directory-row" role="row" key={entry.id}>
                  <span className="media-directory-index">{index + 1}</span>
                  <div className="media-directory-path-picker">
                    <input value={entry.path} onFocus={() => setActiveEntryId(entry.id)} onChange={(event) => updateEntry(entry.id, { path: event.target.value, status: 'idle', error: '' })} placeholder="选择本地媒体目录" />
                    <Button appearance="secondary" icon={<FolderOpen size={15} />} onClick={() => void chooseLocalFolder(entry)}>选择目录</Button>
                  </div>
                  <input className="media-directory-note" value={entry.note} onChange={(event) => updateEntry(entry.id, { note: event.target.value })} placeholder="可选备注" />
                  <span className={`media-entry-status ${entry.status}`}>{entry.status === 'parsing' && <Spinner size="tiny" />}{entry.status === 'parsed' ? '解析完成' : entry.status === 'failed' ? '解析失败' : entry.status === 'parsing' ? '解析中' : '待解析'}</span>
                  <div className="media-entry-actions">
                    <Button
                      className="media-entry-parse-button"
                      appearance="primary"
                      icon={entry.status === 'parsing' ? <Spinner size="tiny" /> : <Play size={15} />}
                      disabled={entry.status === 'parsing'}
                      onClick={() => requestEntryParse(entry)}
                    >
                      {entry.status === 'parsed' ? '重新解析' : '解析并继续'}
                    </Button>
                    <Button
                      className="media-entry-delete-button"
                      appearance="subtle"
                      icon={<Trash2 size={15} />}
                      aria-label="删除目录条目"
                      title="删除此目录"
                      disabled={entry.status === 'parsing'}
                      onClick={() => removeDirectoryEntry(entry.id)}
                    />
                  </div>
                  {entry.error && <small className="media-entry-error">{entry.error}</small>}
                </div>
              ))}
              {entries.length === 0 && <div className="media-directory-empty">尚未添加目录，请点击下方按钮添加。</div>}
            </div>
            )}
            {source === 'local' && <Button appearance="secondary" icon={<Plus size={16} />} onClick={() => { const entry = makeEntry(); setEntries((items) => [...items, entry]); setActiveEntryId(entry.id); }}>添加目录条目</Button>}
            {uploadMessage && <div className="media-preset-progress">{(uploadingTree || scanningFolder) && <Spinner size="tiny" />}<span>{uploadMessage}</span></div>}
            </>)}
            {!importModeActive && (
              <section className="media-preset-section" aria-label="媒体库导入档案">
                <div className="media-preset-heading"><div><span className="media-import-step-label">待处理</span><h3>待处理导入</h3><span>{presetsLoading ? '正在读取…' : pendingPresets.length ? `共 ${pendingPresets.length} 个；可以继续处理或删除。` : '没有待处理的导入。'}</span></div></div>
                {pendingPresets.length > 0 && renderPresetCards(pendingPresets)}
                <div className="media-preset-heading media-preset-ready-heading"><div><span className="media-import-step-label">正式媒体库</span><h3>已建立媒体库</h3><span>刮削完成后才会进入正式媒体库；这里只统计已经建立作品索引的档案。</span></div></div>
                {indexedPresets.length > 0 ? renderPresetCards(indexedPresets) : <div className="media-empty">尚无完成刮削的媒体库。</div>}
              </section>
            )}
        </section>
      )}

      {step === 'confirm' && preview && (
        <section className="media-stage-shell media-confirm-stage">
          <MediaStageHeader
            icon={<ClipboardCheck size={21} />}
            eyebrow="第 2 步"
            title="确认内容"
            description="检查识别结果；低置信项目会先进入后台流程，之后仍可在媒体管理中处理。"
            action={<div className="media-confirm-command"><span>{activeEntry?.pathValidation?.ok === false ? '视频目录当前不可用' : blockingPreviewIssues.length ? `${blockingPreviewIssues.length} 个问题必须处理` : reviewItems.length ? `${reviewItems.length} 项可稍后处理` : '内容已经准备好'}</span></div>}
          />
          <p className="media-plan-overview">{source === 'local' ? '本地目录' : source === 'pan115' ? '115 目录树' : source === 'openlist' ? 'OpenList 目录' : '百度网盘目录树'} · {family === 'anime' ? '动画' : '剧集'} · {importScope === 'seasonal' ? '新番（追更中）' : '已完结'} · {entries.filter((item) => item.status === 'parsed').length} 个已解析目录条目 · {activeEntry?.pathValidation?.ok === false ? '实际视频路径当前不可用' : activeEntry?.pathValidation?.ok ? '实际视频路径已验证' : '生成镜像时验证路径'}</p>
          <div className="media-plan-keyfacts" aria-label="导入内容摘要">
            <div><strong>{preview.summary.work_count}</strong><span>部作品</span></div>
            <div><strong>{preview.summary.video_count}</strong><span>个视频</span></div>
            <div className={preview.summary.needs_review_count > 0 ? 'needs-attention' : ''}><strong>{preview.summary.needs_review_count}</strong><span>项需处理</span></div>
          </div>
          {source !== 'local' && <PathValidationNotice resolvedRoot={activeEntry?.resolvedRoot || ''} validation={activeEntry?.pathValidation} repairLabel={source === 'openlist' ? '重新扫描远端目录' : undefined} onRepair={activeEntry?.presetId ? () => { const preset = presets.find((item) => item.preset_id === activeEntry.presetId); if (!preset) return; if (source === 'openlist') { void rescanOpenlistPreset(preset); } else { void rebindPresetRoot(preset); } } : undefined} repairing={repairingPresetId === activeEntry?.presetId} />}
          <div className="media-confirm-decision-layout">
            <MediaPlanSummary preview={preview} />
            <section className="media-review-section">
              <div className="media-review-heading"><div><h3>{blockingPreviewIssues.length ? '必须处理的问题' : reviewItems.length ? '待处理识别结果' : '识别结果'}</h3><p>{blockingPreviewIssues.length ? '这些问题会影响镜像归位，完成修正后才能继续。' : reviewItems.length ? '这些项目不会阻塞导入，可现在处理，也可以先继续，之后从媒体管理进入处理。' : '没有发现需要补充的识别结果。'}</p></div><span>{blockingPreviewIssues.length || reviewItems.length}</span></div>
              {reviewItems.length
                ? <div className="media-review-list">{reviewItems.map((item) => <article key={item.id}><div><strong>{item.work_title || item.series_group || '未命名作品'}</strong><span>{reviewReasons.get(item.id)?.[0] || item.warnings[0] || item.reasons[0] || '识别信息需要确认'}</span></div><div><span>{item.group_type || '未分类'} · {item.season_number == null ? '季度未知' : `第 ${item.season_number} 季`}</span><Button appearance="secondary" icon={<Wrench size={15} />} onClick={() => openEditor(item)}>现在处理</Button></div></article>)}</div>
                : !blockingPreviewIssues.length && <div className="media-confirm-empty"><CheckCircle2 size={19} /><div><strong>可以继续</strong><span>所有作品都已完成识别。</span></div></div>}
              {blockingPreviewIssues.length > 0 && <div className="media-review-blocking-list">{blockingPreviewIssues.map((issue) => <p key={issue.code}><TriangleAlert size={15} /><span>{issue.message}</span></p>)}</div>}
              <div className="media-confirm-decision-action">
                <Button className="media-primary-command" appearance="primary" icon={<ChevronRight size={16} />} disabled={blockingPreviewIssues.length > 0 || (preview.status !== 'draft' && preview.status !== 'confirmed') || activeEntry?.pathValidation?.ok === false} onClick={() => void confirmPlan()}>{reviewItems.length ? '先确认并继续' : '确认并继续'}</Button>
              </div>
            </section>
          </div>
          {preview.parse_logs.length > 0 && (
            <details className="media-inline-details media-log-details">
              <summary>解析摘要</summary>
              <MediaLogList logs={preview.parse_logs} ariaLabel="解析摘要" limit={12} />
            </details>
          )}
        </section>
      )}

      {step === 'workbench' && (preview || isScrapeTask(task)) && <MediaTaskWorkbench
        mode={isScrapeTask(task) && taskKind === 'scrape' ? 'scrape' : 'mirror'}
        title="创建媒体库并补充资料"
        description="根据确认内容生成媒体库并自动补充资料。开始前最多抽样验证 3 个代表视频，镜像完整后自动开始刮削。"
        task={task}
        logs={taskLogs}
        onStart={handleWorkbenchStart}
        onNewImport={beginNewImport}
        onCancel={activeTask ? () => void cancelTask() : undefined}
        startLabel={isScrapeTask(task) && taskKind === 'scrape' ? '开始补充资料' : '创建媒体库'}
        disabled={isScrapeTask(task) && taskKind === 'scrape'
          ? !preview
          : !preview || preview.status !== 'confirmed' || activeEntry?.pathValidation?.ok === false}
      />}
      {step === 'maintenance' && <LibraryMaintenancePanel onCleared={() => loadPresets(true)} />}

      {editingItem && <div className="media-edit-backdrop" role="presentation"><section className="media-edit-dialog" role="dialog" aria-modal="true" aria-label="修正导入条目"><header><h2>处理识别结果</h2><Button appearance="subtle" onClick={() => setEditingItem(null)}>关闭</Button></header><label>作品名称<input value={editDraft.work_title} onChange={(event) => setEditDraft((value) => ({ ...value, work_title: event.target.value }))} /></label><div className="media-edit-grid"><label>分组<select value={editDraft.group_type} onChange={(event) => setEditDraft((value) => ({ ...value, group_type: event.target.value }))}><option value="season">季度</option><option value="special">特别篇</option><option value="movie">电影</option><option value="ignored">忽略</option></select></label><label>季度<input type="number" value={editDraft.season_number} onChange={(event) => setEditDraft((value) => ({ ...value, season_number: event.target.value }))} /></label><label>集数<input type="number" value={editDraft.episode_number} onChange={(event) => setEditDraft((value) => ({ ...value, episode_number: event.target.value }))} /></label></div><footer><Button appearance="secondary" onClick={() => setEditingItem(null)}>取消</Button><Button appearance="primary" onClick={() => void saveItem()}>保存处理结果</Button></footer></section></div>}
      {pendingSeasonalImport && <div className="media-edit-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPendingSeasonalImport(null); }}>
        <section className="media-edit-dialog media-seasonal-confirm-dialog" role="alertdialog" aria-modal="true" aria-label="确认新番导入">
          <header><div><span className="media-import-step-label">高风险分类</span><h2>确认按新番导入？</h2></div></header>
          <div className="media-seasonal-confirm-summary"><TriangleAlert size={22} /><div><strong>{sourceOptions.find((item) => item.value === source)?.label} · 动画 · 新番（追更中）</strong><span>此次识别出的作品会进入新番追更范围。只有仍在更新的作品才应使用该分类。</span></div></div>
          <p>如果这批内容大多数已经完结，请取消并把“作品状态”改为“已完结（推荐）”。</p>
          <footer><Button appearance="secondary" onClick={() => setPendingSeasonalImport(null)}>取消，返回检查</Button><Button className="media-danger-confirm" appearance="primary" icon={<TriangleAlert size={16} />} onClick={confirmSeasonalImport}>确认按新番导入</Button></footer>
        </section>
      </div>}
      {reviewPreset && <div className="media-edit-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !reviewBusyId) setReviewPreset(null); }}>
        <section className="media-scrape-review-dialog" role="dialog" aria-modal="true" aria-label="处理刮削匹配">
          <header><div><span className="media-import-step-label">人工匹配</span><h2>{getPresetDisplayName(reviewPreset)}</h2><p>为无法自动确认的作品选择正确候选；确认后会自动进入刮削队列。</p></div><Button appearance="subtle" disabled={Boolean(reviewBusyId)} onClick={() => setReviewPreset(null)}>关闭</Button></header>
          {reviewLoading ? <div className="media-review-loading"><Spinner /><span>正在读取待处理作品…</span></div> : scrapeReviewItems.length === 0 ? <div className="media-empty"><CheckCircle2 size={18} />没有待处理的刮削匹配。</div> : <div className="media-scrape-review-list">{scrapeReviewItems.map((item) => {
            const candidates = reviewCandidates[item.scrape_target_id] || item.candidates || [];
            const busy = reviewBusyId === item.scrape_target_id;
            return <article key={item.scrape_target_id}>
              <div className="media-scrape-review-head"><div><strong>{item.local_title}</strong><span>{item.reason || '自动匹配置信度不足'} · {item.scrape_year || '年份未知'} · {item.local_season_number ? `第 ${item.local_season_number} 季` : '季度未知'}</span></div><Button appearance="subtle" disabled={Boolean(reviewBusyId)} onClick={() => void skipReviewItem(item)}>跳过此作品</Button></div>
              <div className="media-scrape-review-search"><input value={reviewQueries[item.scrape_target_id] || ''} onChange={(event) => setReviewQueries((current) => ({ ...current, [item.scrape_target_id]: event.target.value }))} placeholder="输入更准确的作品名称" /><Button appearance="secondary" icon={busy ? <Spinner size="tiny" /> : <SearchCheck size={15} />} disabled={Boolean(reviewBusyId)} onClick={() => void searchReviewCandidates(item)}>重新搜索</Button></div>
              {candidates.length ? <div className="media-scrape-candidates">{candidates.slice(0, 8).map((candidate) => {
                const titles = candidateDisplayTitles(item.local_title, candidate);
                const secondary = titles.secondary || (candidate.tmdb_type === 'movie' ? '电影' : '剧集');
                return <button type="button" disabled={Boolean(reviewBusyId)} key={`${candidate.provider}-${candidate.tmdb_type}-${candidate.tmdb_id}`} onClick={() => void selectReviewCandidate(item, candidate)}><span><strong>{titles.primary}</strong><small>{secondary} · {candidate.year || '年份未知'}</small></span><em>{formatCandidateScore(candidate.score || 0)}</em></button>;
              })}</div> : <div className="media-empty">没有合适候选，请修改名称后重新搜索，或跳过此作品。</div>}
            </article>;
          })}</div>}
        </section>
      </div>}
      {deletingPreset && (
        <div className="media-edit-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !deletingPresetId) closePresetDelete();
        }}>
          <section className="media-edit-dialog media-delete-dialog" role="alertdialog" aria-modal="true" aria-label="删除导入卡片">
            <header><div><span className="media-import-step-label">删除导入卡片</span><h2>删除“{getPresetDisplayName(deletingPreset)}”？</h2></div></header>
            <p>先确认影响范围。此操作只清理用于导入比对的卡片信息。</p>
            <div className="media-delete-options">
              <div className="media-delete-core-option"><Trash2 size={16} /><span><strong>删除卡片与目录树版本</strong><small>删除这张导入卡片，以及 KumiPlayer 保存的目录树比对档案。</small></span></div>
            </div>
            <div className="media-delete-preserved"><CheckCircle2 size={17} /><span>镜像、NFO、图片、刮削结果、媒体库、追更和观看状态均会保留。如需清理这些数据，请前往“媒体库维护”按来源操作。</span></div>
            {presetDeletePreview && (
              <div className={`media-preset-delete-preview${presetDeletePreview.blocked ? ' blocked' : ''}`}>
                <strong>{presetDeletePreview.blocked ? '安全检查未通过' : '删除影响预览'}</strong>
                <span>导入卡片 1 张</span>
                <span>目录树版本 {presetDeletePreview.archive_version_count} 个</span>
                {presetDeletePreview.warnings.map((warning) => <small key={warning}>{warning}</small>)}
              </div>
            )}
            {deleteDialogError && <div className="media-delete-dialog-error" role="alert"><TriangleAlert size={17} /><span>{deleteDialogError}</span></div>}
            <footer>
              <Button appearance="secondary" disabled={Boolean(deletingPresetId)} onClick={closePresetDelete}>取消</Button>
              {!presetDeletePreview ? (
                <Button appearance="primary" icon={deletePreviewLoading ? <Spinner size="tiny" /> : <SearchCheck size={15} />} disabled={deletePreviewLoading} onClick={() => void preparePresetDelete()}>{deletePreviewLoading ? '正在生成预览' : '生成删除预览'}</Button>
              ) : (
                <Button className="media-danger-confirm" appearance="primary" icon={deletingPresetId ? <Spinner size="tiny" /> : <Trash2 size={15} />} disabled={Boolean(deletingPresetId) || presetDeletePreview.blocked} onClick={() => void removePreset()}>{deletingPresetId ? '正在删除' : '确认删除卡片'}</Button>
              )}
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}

function getPresetDisplayName(preset: MediaLibraryPreset) {
  if (preset.source === 'local') return preset.name;
  return preset.import_scope === 'seasonal'
    ? '新番库'
    : preset.import_family === 'anime' ? '动画库' : '剧集库';
}

function getPresetOrdinalLabel(preset: MediaLibraryPreset) {
  if (preset.source === 'local') return '';
  const suffix = preset.name.match(/([一二三四五六七八九十\d]+)$/)?.[1] || '';
  if (!suffix) return '';
  if (/^\d+$/.test(suffix)) return `#${Number(suffix)}`;
  const number = {
    一: 1,
    二: 2,
    三: 3,
    四: 4,
    五: 5,
    六: 6,
    七: 7,
    八: 8,
    九: 9,
    十: 10,
  }[suffix as '一' | '二' | '三' | '四' | '五' | '六' | '七' | '八' | '九' | '十'];
  return number ? `#${number}` : '';
}

function getPresetLifecycleLabel(status: MediaLibraryPreset['lifecycle_status']) {
  return {
    draft: '待确认识别结果',
    confirmed: '已确认 · 待生成镜像',
    mirrored: '镜像已生成 · 待刮削',
    needs_attention: '刮削需人工处理',
    ready: '刮削完成 · 已建立索引',
  }[status] || '待处理';
}

function PathValidationNotice({ resolvedRoot, validation, onRepair, repairing = false, repairLabel }: { resolvedRoot: string; validation?: SourcePathValidation; onRepair?: () => void; repairing?: boolean; repairLabel?: string }) {
  const failed = validation?.ok === false;
  return <div className={`media-path-validation ${failed ? 'failed' : validation?.ok ? 'verified' : 'unknown'}`}>
    <div>
      {failed ? <TriangleAlert size={18} /> : <CheckCircle2 size={18} />}
      <span><strong>{failed ? '路径验证失败' : validation?.ok ? '实际视频路径已验证' : '生成镜像时将验证路径'}</strong><small>{validation?.message || '旧版导入尚无验证记录，生成镜像前会强制抽样检查。'}</small></span>
    </div>
    <dl>
      <div><dt>实际视频根目录</dt><dd title={resolvedRoot || validation?.resolved_root || ''}>{resolvedRoot || validation?.resolved_root || '等待验证'}</dd></div>
      {validation?.example_path && <div><dt>抽样视频路径</dt><dd title={validation.example_path}>{validation.example_path}</dd></div>}
    </dl>
    {failed && onRepair && <div className="media-path-validation-actions"><Button appearance="secondary" icon={repairing ? <Spinner size="tiny" /> : <FolderOpen size={15} />} disabled={repairing} onClick={onRepair}>{repairLabel || '选择实际文件夹并重新验证'}</Button></div>}
  </div>;
}

function isScrapeTask(task: TaskRecord | null): boolean {
  return Boolean(task?.task_type.startsWith('scrape_'));
}

function openlistPhaseLabel(phase: string | undefined, fallbackMessage?: string): string {
  const labels: Record<string, string> = {
    remote_scan: '正在扫描远端目录',
    manifest_write: '目录树读取完成，正在保存扫描清单',
    local_validate: '正在验证本地挂载文件',
    plan_build: '正在识别作品并生成导入计划',
    complete: '扫描完成',
  };
  return (phase && labels[phase]) || fallbackMessage || '正在扫描远端目录';
}
