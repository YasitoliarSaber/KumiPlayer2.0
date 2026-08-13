import { useEffect, useState } from 'react';
import { Button, ProgressBar, Spinner } from '@fluentui/react-components';
import {
  CheckCircle2,
  Download,
  FolderOpen,
  Layers3,
  Play,
  RefreshCw,
  Sparkles,
  Square,
  TriangleAlert,
} from 'lucide-react';
import type { TaskRecord } from '../../api/types';
import MediaLogList, { type MediaLog } from './MediaLogList';
import MediaStageHeader from './MediaStageHeader';

type Props = {
  mode: 'mirror' | 'scrape';
  title: string;
  description: string;
  task: TaskRecord | null;
  logs: MediaLog[];
  onStart: () => void;
  onNewImport?: () => void;
  onCancel?: () => void;
  startLabel: string;
  disabled: boolean;
};

type ExecutionMetrics = {
  succeeded: number;
  failed: number;
  skipped: number;
  total: number | '-';
  hasMirrorResult: boolean;
};

const SUB_STAGES: Array<{ key: string; label: string }> = [
  { key: 'validate', label: '路径抽样验证' },
  { key: 'mirror', label: '镜像生成' },
  { key: 'verify', label: '镜像结果校验' },
  { key: 'scrape', label: '资料匹配/下载' },
];

export function isMirrorTaskReady(task: TaskRecord | null): boolean {
  const metrics = getExecutionMetrics(task, 'mirror');
  return task?.status === 'succeeded'
    && metrics.hasMirrorResult
    && metrics.total !== '-'
    && metrics.total > 0
    && metrics.failed === 0
    && metrics.succeeded + metrics.skipped === metrics.total;
}

function currentSubStage(mode: 'mirror' | 'scrape', task: TaskRecord | null): number {
  if (mode === 'scrape') return 3;
  if (task?.status === 'succeeded') return isMirrorTaskReady(task) ? 3 : 2;
  if (task?.status === 'failed' || task?.status === 'cancelled') return 2;
  if (task && (task.status === 'pending' || task.status === 'running')) {
    const metrics = getExecutionMetrics(task, 'mirror');
    return metrics.hasMirrorResult && metrics.total !== '-' ? 1 : 0;
  }
  return 0;
}

export default function MediaTaskWorkbench({
  mode,
  title,
  description,
  task,
  logs,
  onStart,
  onNewImport,
  onCancel,
  startLabel,
  disabled,
}: Props) {
  const progress = Math.max(0, Math.min(100, Number(task?.progress || 0)));
  const active = task?.status === 'running' || task?.status === 'pending';
  const isStopped = task?.status === 'cancelled' || (task?.status === 'failed' && task?.message === '已停止');
  const [nowMs, setNowMs] = useState(Date.now());
  const [stopping, setStopping] = useState(false);
  const metrics = getExecutionMetrics(task, mode);
  const result = taskResultRecord(task);
  const stoppedRemaining = numericResult(
    result,
    ['remaining_targets'],
    typeof metrics.total === 'number'
      ? Math.max(0, metrics.total - metrics.succeeded - metrics.failed - metrics.skipped)
      : 0,
  );
  const mirrorReady = mode === 'mirror' && isMirrorTaskReady(task);
  const mirrorTerminal = mode === 'mirror' && (task?.status === 'succeeded' || task?.status === 'failed');
  const elapsed = formatTaskElapsed(task, active ? nowMs : undefined);
  const currentTarget = stringResult(result, ['current_target']);
  const completedTargets = Number(result.completed_targets ?? 0);
  const totalTargets = Number(result.total_targets ?? 0);
  const remainingTargets = Number(result.remaining_targets ?? 0);
  const subStage = currentSubStage(mode, task);

  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, task?.task_id]);

  useEffect(() => {
    if (!active) setStopping(false);
  }, [active]);

  const handleStop = () => {
    if (!onCancel || stopping) return;
    setStopping(true);
    onCancel();
  };

  const values: Array<[string, number | '-']> = [
    [mode === 'mirror' ? '新建' : '成功', metrics.succeeded],
    ['失败', metrics.failed],
    [mode === 'mirror' ? '已存在' : '跳过', metrics.skipped],
    ['总数', metrics.total],
  ];
  const terminalSuccess = task?.status === 'succeeded' && (mode !== 'mirror' || mirrorReady);
  const statusCopy = terminalSuccess
    ? mode === 'mirror' ? '媒体库已创建' : '资料补充完成'
    : active ? mode === 'mirror' ? '正在生成镜像' : '正在补充资料'
      : isStopped ? '已停止' : task?.status === 'failed' ? (mode === 'mirror' ? '镜像生成失败' : '处理失败') : '准备就绪';
  const needsRetry = task?.status === 'failed' || task?.status === 'cancelled';
  const primaryLabel = needsRetry
    ? mode === 'mirror' ? '重新创建' : task?.status === 'cancelled' ? '继续补充资料' : '重新补充资料'
    : startLabel;
  const autoAdvancing = mode === 'mirror' && mirrorReady && !active;
  const errorLogs = logs.filter((log) => log.kind === 'warn' || log.kind === 'error');
  const hasDiagnostics = errorLogs.length > 0 || Boolean(task?.error);

  const exportErrorLogs = () => {
    const content = buildErrorLogExport(title, errorLogs, task?.error);
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `KumiPlayer-错误日志-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="media-task-workbench">
      <MediaStageHeader
        icon={mode === 'mirror' ? <Layers3 size={21} /> : <Sparkles size={21} />}
        eyebrow="第 3 步"
        title={title}
        description={description}
        status={terminalSuccess ? <span className="media-success"><CheckCircle2 size={16} />已完成</span> : undefined}
      />

      <div className="media-workbench-layout">
        <div className="media-workbench-main">
          <ol className="media-workbench-substages" aria-label="执行阶段">
            {SUB_STAGES.map((stage, index) => (
              <li
                key={stage.key}
                className={index < subStage ? 'complete' : index === subStage ? 'active' : ''}
                aria-current={index === subStage ? 'step' : undefined}
              >
                <span>{index + 1}</span>
                <span>{stage.label}</span>
              </li>
            ))}
          </ol>

          <div className="media-task-overview">
            <div className={`media-task-state-icon ${terminalSuccess ? 'success' : active ? 'active' : ''}`}>
              {terminalSuccess ? <CheckCircle2 size={24} /> : active ? <Spinner size="small" /> : mode === 'mirror' ? <Layers3 size={23} /> : <Sparkles size={23} />}
            </div>
            <div className="media-task-progress-copy">
              <div className="media-task-status"><strong>{statusCopy}</strong><span>{task?.message || '点击下方按钮开始，任务会在后台持续执行。'}</span></div>
              {currentTarget && <div className="media-task-current" title={currentTarget}>{currentTarget}</div>}
              <ProgressBar value={progress / 100} max={1} />
            </div>
            <strong className="media-task-percent">{task ? `${Math.round(progress)}%` : '—'}</strong>
          </div>

          {mirrorTerminal && (
            <div className={`media-flow-alert ${mirrorReady ? 'success' : 'error'}`}>
              {mirrorReady ? <CheckCircle2 size={16} /> : <TriangleAlert size={16} />}
              <span>
                <strong>{mirrorReady
                  ? metrics.succeeded === 0 && metrics.skipped > 0 ? '已存在且无需重写' : '镜像已就绪'
                  : '创建没有完成'}</strong>
                {mirrorReady
                  ? `已新建 ${metrics.succeeded} 项，复用 ${metrics.skipped} 项，共 ${metrics.total} 项。`
                  : `${task?.error || task?.message || '任务结果统计不完整'}。`}
              </span>
            </div>
          )}
          {isStopped && mode === 'scrape' && (
            <div className="media-flow-alert stopped">
              <Square size={15} />
              <span><strong>任务已停止</strong>已保留完成的 {metrics.succeeded} 项，剩余 {stoppedRemaining} 项可稍后继续。</span>
            </div>
          )}
          {task?.status === 'failed' && !isStopped && mode === 'scrape' && (
            <div className="media-flow-alert error">
              <TriangleAlert size={16} />
              <span><strong>处理失败</strong>{task.error || task.message}</span>
            </div>
          )}

          <div className="media-stage-actions">
            {mode === 'scrape' && task?.status === 'succeeded' && onNewImport
              && <Button className="media-new-import-command" appearance="secondary" icon={<FolderOpen size={15} />} onClick={onNewImport}>导入新目录</Button>}
            {onCancel && active && (
              <Button appearance="secondary" icon={<Square size={15} />} disabled={stopping} onClick={handleStop}>
                {stopping ? '正在停止' : '停止'}
              </Button>
            )}
            {autoAdvancing
              ? <span className="media-auto-advance"><Spinner size="tiny" />正在自动开始补充资料…</span>
              : <Button className="media-primary-command" appearance="primary" onClick={onStart} icon={active ? <Spinner size="tiny" /> : needsRetry ? <RefreshCw size={15} /> : <Play size={15} />} disabled={disabled || active}>{primaryLabel}</Button>}
          </div>
        </div>

        <aside className="media-workbench-side">
          <section className="media-workbench-metrics" aria-label="任务统计">
            <header><h3>任务统计</h3></header>
            <div className="media-task-metrics">
              {values.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
              {mode === 'scrape' && <div className="media-task-duration"><span>本次耗时</span><strong>{elapsed}</strong></div>}
            </div>
          </section>
        </aside>
      </div>

      <section className="media-log-panel" aria-label="执行记录">
        <header>
          <h3>执行记录</h3>
          <div className="media-log-panel-actions">
            {mode === 'scrape' && totalTargets > 0 && <small>已处理 {completedTargets} / {totalTargets} · 剩余 {remainingTargets}</small>}
            <Button appearance="subtle" size="small" icon={<Download size={14} />} disabled={!hasDiagnostics} onClick={exportErrorLogs}>
              导出错误日志{hasDiagnostics ? ` (${errorLogs.length + (task?.error ? 1 : 0)})` : ''}
            </Button>
          </div>
        </header>
        {logs.length
          ? <MediaLogList logs={logs} ariaLabel="执行记录" limit={30} variant={mode === 'scrape' ? 'work-summary' : 'timeline'} />
          : active
            ? <p className="media-log-running"><Spinner size="tiny" />任务正在运行，最新状态会显示在上方。</p>
            : <p className="media-log-empty">当前没有执行记录。</p>}
      </section>
    </section>
  );
}

export function buildErrorLogExport(title: string, logs: MediaLog[], taskError = ''): string {
  const lines = [
    'KumiPlayer 导入错误日志',
    `任务：${title}`,
    `导出时间：${new Date().toLocaleString('zh-CN')}`,
    '',
  ];
  for (const log of logs.filter((item) => item.kind === 'warn' || item.kind === 'error')) {
    const level = log.kind === 'error' ? '错误' : '警告';
    lines.push(`[${log.time || '无时间'}] [${level}] ${log.message || '无详细信息'}`);
  }
  if (taskError.trim() && !logs.some((log) => log.kind === 'error' && log.message === taskError)) {
    lines.push(`[无时间] [错误] ${taskError}`);
  }
  return `${lines.join('\n')}\n`;
}

function taskResultRecord(task: TaskRecord | null): Record<string, unknown> {
  return task?.result && typeof task.result === 'object'
    ? task.result as Record<string, unknown>
    : {};
}

function numericResult(result: Record<string, unknown>, keys: string[], fallback = 0): number {
  for (const key of keys) {
    const value = Number(result[key]);
    if (Number.isFinite(value)) return value;
  }
  return fallback;
}

function stringResult(result: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = result[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function getExecutionMetrics(task: TaskRecord | null, mode: 'mirror' | 'scrape'): ExecutionMetrics {
  const result = taskResultRecord(task);
  if (mode === 'mirror') {
    const succeeded = numericResult(result, ['generated_count', 'mirror_count', 'strm_count']);
    const failed = numericResult(result, ['failed_count', 'failed']);
    const skipped = numericResult(result, ['skipped_count', 'skipped_existing', 'skipped']);
    const reportedTotal = numericResult(result, ['items_count', 'total'], succeeded + failed + skipped);
    return {
      succeeded,
      failed,
      skipped,
      total: reportedTotal,
      hasMirrorResult: ['generated_count', 'failed_count', 'skipped_count', 'items_count']
        .every((key) => Object.hasOwn(result, key)),
    };
  }
  const succeeded = numericResult(result, ['auto_scraped', 'succeeded', 'success']);
  const failed = numericResult(result, ['failed', 'failed_count']);
  const skipped = numericResult(result, ['skipped_existing', 'skipped', 'skipped_count']);
  const rawTotal = result.total_targets ?? result.total;
  return {
    succeeded,
    failed,
    skipped,
    total: rawTotal == null ? '-' : numericResult(result, ['total_targets', 'total']),
    hasMirrorResult: false,
  };
}

function formatTaskElapsed(task: TaskRecord | null, nowMs = Date.now()) {
  if (!task?.started_at) return '未开始';
  const started = Date.parse(task.started_at);
  if (!Number.isFinite(started)) return '计算中';
  const finished = task.finished_at ? Date.parse(task.finished_at) : NaN;
  const end = Number.isFinite(finished) ? finished : nowMs;
  const seconds = Math.max(0, Math.floor((end - started) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${rest} 秒`;
  return `${rest} 秒`;
}
