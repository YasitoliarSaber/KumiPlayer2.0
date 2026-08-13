import { useEffect, useMemo, useState } from 'react';
import { Button, Spinner, Tooltip } from '@fluentui/react-components';
import { AlertTriangle, Database, ListVideo, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import {
  libraryApi,
  type DeletePreviewResponse,
  type LibraryDeleteSource,
  type LibraryDiagnosticItem,
  type LibraryDiagnosticsResponse,
} from '../../api/library';
import { tasksApi } from '../../api/tasks';
import { useLibraryStore } from '../../stores/library';
import { useUiStore } from '../../stores/ui';
import { mainEpisodeCount } from '../../utils/workStats';

type Operation = { phase: 'idle' | 'running' | 'succeeded' | 'failed'; title: string; detail: string };
type DisplayDiagnosticItem = LibraryDiagnosticItem & { severity: 'error' | 'warning' };

const sourceLabels: Record<LibraryDeleteSource, string> = {
  all: '全部来源',
  pan115: '115 网盘',
  baidu: '百度网盘',
  openlist: 'OpenList 连接',
  local: '本地',
};

export default function LibraryMaintenancePanel({ onCleared }: { onCleared?: () => Promise<void> | void }) {
  const selectedSource = useUiStore((state) => state.source) as LibraryDeleteSource;
  const setSelectedSource = useUiStore((state) => state.setSource);
  const works = useLibraryStore((state) => state.works);
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const [preview, setPreview] = useState<DeletePreviewResponse | null>(null);
  const [diagnostics, setDiagnostics] = useState<LibraryDiagnosticsResponse | null>(null);
  const [operation, setOperation] = useState<Operation>({ phase: 'idle', title: '', detail: '' });
  const [busy, setBusy] = useState<'refresh' | 'rescan' | 'preview' | 'confirm' | ''>('');

  const visibleWorks = useMemo(
    () => selectedSource === 'all' ? works : works.filter((work) => workIncludesSource(work, selectedSource)),
    [selectedSource, works],
  );
  const episodeCount = useMemo(
    () => visibleWorks.reduce((total, work) => total + mainEpisodeCount(work), 0),
    [visibleWorks],
  );
  const diagnosticItems = useMemo<DisplayDiagnosticItem[]>(() => [
    ...(diagnostics?.errors || []).map((item) => ({ ...item, severity: 'error' as const })),
    ...(diagnostics?.warnings || []).map((item) => ({ ...item, severity: 'warning' as const })),
  ], [diagnostics]);
  const affectedWorkCount = useMemo(
    () => new Set(diagnosticItems.map((item) => item.library_work_id || item.scrape_target_id || item.scrape_title).filter(Boolean)).size,
    [diagnosticItems],
  );
  const warningCount = diagnosticItems.length;
  const deleteScopeLabel = selectedSource === 'all'
    ? '全部媒体库'
    : `${sourceLabels[selectedSource]}媒体库`;

  const refreshStatus = async () => {
    const source = selectedSource === 'all' ? undefined : selectedSource;
    setDiagnostics(await libraryApi.getDiagnostics(source));
  };

  useEffect(() => {
    setPreview(null);
    setOperation({ phase: 'idle', title: '', detail: '' });
    void refreshStatus().catch((error: Error) => {
      setOperation({ phase: 'failed', title: '状态读取失败', detail: error.message });
    });
  }, [selectedSource]);

  const run = async (kind: typeof busy, action: () => Promise<void>) => {
    if (busy) return;
    setBusy(kind);
    try {
      await action();
    } catch (error) {
      setOperation({ phase: 'failed', title: '操作未完成', detail: (error as Error).message });
    } finally {
      setBusy('');
    }
  };

  const rescan = () => run('rescan', async () => {
    const previousCount = visibleWorks.length;
    setOperation({ phase: 'running', title: `正在同步${sourceLabels[selectedSource]}`, detail: '后台正在重新读取已确认的导入计划和镜像文件。' });
    const result = await libraryApi.rescanLibrary(selectedSource === 'all' ? undefined : selectedSource);
    await waitForTask(result.task_id, (message) => {
      setOperation({ phase: 'running', title: `正在同步${sourceLabels[selectedSource]}`, detail: message });
    });
    await Promise.all([loadLibrary({ force: true }), refreshStatus()]);
    const refreshedWorks = useLibraryStore.getState().works;
    const currentCount = selectedSource === 'all'
      ? refreshedWorks.length
      : refreshedWorks.filter((work) => workIncludesSource(work, selectedSource)).length;
    const countChange = currentCount === previousCount
      ? `当前范围仍为 ${currentCount} 部作品。`
      : `当前范围由 ${previousCount} 部变为 ${currentCount} 部。`;
    setOperation({ phase: 'succeeded', title: '媒体库已同步', detail: `${countChange} 同步只刷新索引，不会自动删除镜像或源视频。` });
  });

  const buildPreview = () => run('preview', async () => {
    const result = await libraryApi.deleteLibraryPreview(selectedSource);
    setPreview(result);
    setOperation({
      phase: result.blocked ? 'failed' : 'succeeded',
      title: result.blocked ? '安全检查未通过' : '清理预览已生成',
      detail: result.blocked ? (result.warnings[0] || '存在不可安全删除的文件') : '请核对来源与文件数量，再进行最终确认。',
    });
  });

  const confirmClear = () => run('confirm', async () => {
    if (!preview) throw new Error('请先生成清理预览');
    if (preview.blocked) throw new Error('当前预览包含风险项，已阻止清理');
    if (preview.source !== selectedSource) throw new Error('删除范围已经变化，请重新生成清理预览');
    setOperation({ phase: 'running', title: `正在删除${deleteScopeLabel}`, detail: '正在清理所选范围的 KumiPlayer 生成内容和导入档案，不会删除源视频或外部原始目录树文件。' });
    const result = await libraryApi.deleteLibraryConfirm(preview.preview_id);
    setPreview(null);
    await Promise.all([loadLibrary({ force: true }), refreshStatus(), onCleared?.()]);
    setOperation({
      phase: result.failed.length ? 'failed' : 'succeeded',
      title: result.failed.length ? '清理完成，但有未完成项' : '清理完成',
      detail: `已删除媒体库${result.deleted_catalog_root_count ? `，并清理相关来源目录与导入记录（${result.deleted_catalog_root_count} 个来源目录、${result.deleted_catalog_batch_count} 次导入记录、${result.deleted_catalog_unit_count} 个后台识别单元）` : ''}：从索引移除 ${result.deleted_library_work_count} 部媒体库作品，删除 ${result.deleted.length} 项生成内容、${result.deleted_preset_ids.length} 个目录树导入档案、${result.deleted_tracking_binding_count} 条追更记录及 ${result.deleted_tracking_scan_run_count} 条扫描历史${result.cancelled_tracking_task_count ? `，停止 ${result.cancelled_tracking_task_count} 个相关扫描任务` : ''}${result.failed.length ? `，${result.failed.length} 项失败` : ''}。`,
    });
  });

  const previewFileCount = preview?.files.filter((file) => file.allowed && file.exists).length || 0;

  return (
    <section className="library-maintenance-stage" aria-labelledby="library-maintenance-title">
      <div className="media-flow-section-head maintenance-stage-head">
        <div>
          <h2 id="library-maintenance-title">媒体库维护</h2>
          <p>按来源同步、检查或清理媒体库。</p>
        </div>
        <label className="maintenance-source-field">
          <span>操作范围</span>
          <select disabled={Boolean(busy)} value={selectedSource} onChange={(event) => setSelectedSource(event.target.value as LibraryDeleteSource)}>
            {Object.entries(sourceLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      </div>

      <div className="maintenance-summary-strip" aria-label="媒体库概览">
        <article><span className="maintenance-summary-icon"><Database size={18} /></span><div><span>作品</span><strong>{visibleWorks.length}</strong></div></article>
        <article><span className="maintenance-summary-icon"><ListVideo size={18} /></span><div><span>剧集</span><strong>{episodeCount}</strong></div></article>
        <article className={warningCount ? 'attention' : ''}><span className="maintenance-summary-icon"><ShieldCheck size={18} /></span><div><span>需人工检查</span><strong>{warningCount}</strong></div></article>
      </div>

      <section className="maintenance-command-surface" aria-labelledby="maintenance-sync-title">
        <div className="maintenance-command-copy">
          <span className="maintenance-command-icon"><RefreshCw size={19} /></span>
          <div><h3 id="maintenance-sync-title">索引同步</h3><p>{sourceLabels[selectedSource]} · 重新读取已确认的导入计划和镜像索引</p></div>
        </div>
        <div className="maintenance-card-actions">
          <Tooltip content="刷新状态" relationship="label">
            <Button className="maintenance-command icon-only" appearance="subtle" aria-label="刷新媒体库状态" icon={busy === 'refresh' ? <Spinner size="tiny" /> : <RefreshCw size={17} />} disabled={Boolean(busy)} onClick={() => void run('refresh', async () => { await Promise.all([loadLibrary({ force: true }), refreshStatus()]); })} />
          </Tooltip>
          <Button className="maintenance-command primary" appearance="primary" icon={busy === 'rescan' ? <Spinner size="tiny" /> : <RefreshCw size={16} />} disabled={Boolean(busy)} onClick={() => void rescan()}>同步索引</Button>
        </div>
      </section>

      {operation.phase !== 'idle' && <div className={`maintenance-operation-banner ${operation.phase}`} role="status" aria-live="polite">{operation.phase === 'running' && <Spinner size="tiny" />}<div><strong>{operation.title}</strong><span>{operation.detail}</span></div></div>}

      <section className={`maintenance-diagnostics-card${warningCount ? ' attention' : ''}`} aria-labelledby="maintenance-diagnostics-title">
        <div className="maintenance-diagnostics-head">
          <h3 id="maintenance-diagnostics-title">同步检查</h3>
          <span className="maintenance-diagnostics-count">{warningCount ? `${warningCount} 个诊断项 · 涉及 ${affectedWorkCount} 部作品` : '未发现异常'}</span>
        </div>
        {warningCount ? (
          <div className="maintenance-diagnostics-list">
            {diagnosticItems.map((item, index) => (
              <article className={`maintenance-diagnostic-item ${item.severity}`} key={`${item.code}-${item.scrape_target_id || item.library_work_id || index}-${index}`}>
                <div className="maintenance-diagnostic-title-row">
                  <strong>{item.scrape_title || item.series_group || item.library_work_id || '未识别作品'}</strong>
                  <span>{diagnosticSourceLabel(item.source)}</span>
                  {item.local_season_number != null && <span>本地第 {item.local_season_number} 季</span>}
                  <em>{diagnosticTypeLabel(item.code)}</em>
                </div>
                <p>{item.message}</p>
                <small>{diagnosticSuggestion(item.code)}</small>
                {item.path && <code title={item.path}>{item.path}</code>}
              </article>
            ))}
          </div>
        ) : (
          <div className="maintenance-diagnostics-empty"><ShieldCheck size={18} /><span>索引与刮削记录一致</span></div>
        )}
      </section>

      <section className="maintenance-danger-zone" aria-labelledby="maintenance-danger-title">
        <div className="maintenance-danger-copy"><AlertTriangle size={20} /><div><h3 id="maintenance-danger-title">删除{deleteScopeLabel}</h3><p>清理所选范围的 KumiPlayer 生成内容和导入档案。网盘挂载文件、本地原视频和外部原始 TXT 始终不会被删除。</p></div></div>
        <div className="maintenance-danger-actions">
          <Button className="maintenance-command danger" icon={busy === 'preview' ? <Spinner size="tiny" /> : <Trash2 size={16} />} disabled={Boolean(busy)} onClick={() => void buildPreview()}>生成删除预览</Button>
        </div>
        {preview && (
          <div className={`maintenance-delete-preview${preview.blocked ? ' blocked' : ''}`}>
            <div><span>即将彻底删除</span><strong>{deleteScopeLabel}</strong><small>删除 {preview.library_work_count} 部媒体库作品、处理 {previewFileCount} 个生成文件、{preview.media_preset_count} 个目录树导入档案、{preview.tracking_binding_count} 条追更记录及 {preview.tracking_scan_run_count} 条扫描历史</small>{preview.catalog_root_count > 0 && <small className="maintenance-catalog-summary">同时清理 {preview.catalog_root_count} 个来源目录、{preview.catalog_batch_count} 次导入记录和 {preview.catalog_unit_count} 个后台识别单元</small>}{preview.warnings.map((warning) => <small key={warning}>{warning}</small>)}
              {preview.catalog_root_count > 0 && (
                <details className="maintenance-catalog-details">
                  <summary>展开来源目录与导入记录详情</summary>
                  <dl>
                    <div><dt>来源目录</dt><dd>{preview.catalog_root_count}</dd></div>
                    <div><dt>导入记录</dt><dd>{preview.catalog_batch_count}</dd></div>
                    <div><dt>扫描缓存</dt><dd>{preview.catalog_directory_count} 个目录 / {preview.catalog_node_count} 个条目</dd></div>
                    <div><dt>识别记录</dt><dd>{preview.catalog_unit_count} 个单元 / {preview.catalog_revision_count} 个版本</dd></div>
                    <div><dt>后台任务</dt><dd>{preview.catalog_job_count}{preview.catalog_active_job_count ? `（其中 ${preview.catalog_active_job_count} 个运行中，需先停止）` : ''}</dd></div>
                  </dl>
                </details>
              )}
            </div>
            <Button className="maintenance-command danger" icon={<Trash2 size={16} />} disabled={preview.blocked || Boolean(busy)} onClick={() => void confirmClear()}>确认删除{deleteScopeLabel}</Button>
          </div>
        )}
      </section>
    </section>
  );
}

function diagnosticSourceLabel(source?: string) {
  return sourceLabels[source as LibraryDeleteSource] || source || '未知来源';
}

function workIncludesSource(work: { source?: string; sources?: string[] }, source: LibraryDeleteSource) {
  return work.source === source || work.sources?.includes(source) === true;
}

function diagnosticTypeLabel(code: string) {
  const labels: Record<string, string> = {
    library_index_missing: '索引缺失',
    library_work_missing: '作品索引缺失',
    library_season_missing: '季索引缺失',
    season_tmdb_mismatch: 'TMDB 映射不一致',
    season_target_id_mismatch: '刮削目标不一致',
    nfo_missing: 'NFO 缺失',
    poster_missing: '海报缺失',
    fanart_missing: '背景图缺失',
    clearlogo_missing: 'Logo 缺失',
  };
  return labels[code] || '一致性异常';
}

function diagnosticSuggestion(code: string) {
  if (code === 'library_index_missing' || code === 'library_work_missing' || code === 'library_season_missing' || code === 'season_target_id_mismatch') {
    return '建议：先确认对应来源和导入计划，再重新同步该来源。';
  }
  if (code === 'season_tmdb_mismatch') {
    return '建议：核对作品和季度的 TMDB 绑定，确认后重新自动刮削。';
  }
  if (code.endsWith('_missing')) {
    return '建议：确认下方路径是否可访问；文件确实缺失时，对该作品重新刮削。';
  }
  return '建议：核对作品的来源、季度与刮削记录后，再执行对应修复。';
}

async function waitForTask(taskId: string, onProgress: (message: string) => void) {
  for (let attempt = 0; attempt < 240; attempt += 1) {
    const task = await tasksApi.getLibraryTask(taskId);
    onProgress(task.message || `已处理 ${Math.round(Number(task.progress) || 0)}%`);
    if (task.status === 'succeeded') return;
    if (task.status === 'failed') throw new Error(task.message || '媒体库同步失败');
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  throw new Error('媒体库同步等待超时，请稍后刷新状态');
}
