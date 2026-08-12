import { useMemo, useState } from 'react';
import { FileDown, FolderOpen, RefreshCw, RotateCcw, TriangleAlert } from 'lucide-react';
import {
  isDesktopRuntime,
  openDesktopLogDirectory,
  restartDesktopBackend,
  saveDesktopDiagnostics,
} from '../../platform/desktopRecovery';

interface RecoveryViewProps {
  title: string;
  message: string;
  detail?: string;
  onRetry?: () => void;
}

export default function RecoveryView({ title, message, detail = '', onRetry }: RecoveryViewProps) {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const [busyAction, setBusyAction] = useState('');
  const [status, setStatus] = useState('');

  const run = async (label: string, action: () => Promise<void>) => {
    if (busyAction) return;
    setBusyAction(label);
    setStatus('');
    try {
      await action();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : `${label}失败`);
    } finally {
      setBusyAction('');
    }
  };

  const reload = () => {
    if (onRetry) {
      onRetry();
      return;
    }
    window.location.reload();
  };

  const restart = () => run('重启后端', async () => {
    await restartDesktopBackend();
    setStatus('后端已恢复，正在重新加载');
    window.setTimeout(() => window.location.reload(), 350);
  });

  const exportDiagnostics = () => run('导出诊断', async () => {
    const path = await saveDesktopDiagnostics([
      `KumiPlayer ${__APP_VERSION__}`,
      new Date().toISOString(),
      navigator.userAgent,
      title,
      message,
      detail,
    ].filter(Boolean).join('\n\n'));
    setStatus(`诊断已保存：${path}`);
  });

  return (
    <main className="recovery-screen" role="alert" aria-live="assertive">
      <section className="recovery-panel">
        <div className="recovery-icon" aria-hidden="true"><TriangleAlert size={24} /></div>
        <div className="recovery-copy">
          <span>KumiPlayer</span>
          <h1>{title}</h1>
          <p>{message}</p>
        </div>
        <div className="recovery-actions">
          <button type="button" className="primary" onClick={reload} disabled={Boolean(busyAction)}>
            <RefreshCw size={17} />重新加载
          </button>
          <button type="button" onClick={() => void restart()} disabled={!desktop || Boolean(busyAction)}>
            <RotateCcw size={17} />{busyAction === '重启后端' ? '正在重启' : '重启后端'}
          </button>
          <button type="button" onClick={() => void run('打开日志', openDesktopLogDirectory)} disabled={!desktop || Boolean(busyAction)}>
            <FolderOpen size={17} />打开日志
          </button>
          <button type="button" onClick={() => void exportDiagnostics()} disabled={!desktop || Boolean(busyAction)}>
            <FileDown size={17} />{busyAction === '导出诊断' ? '正在导出' : '导出诊断'}
          </button>
        </div>
        {status && <p className="recovery-status" role="status">{status}</p>}
        {detail && <details className="recovery-detail"><summary>错误详情</summary><pre>{detail}</pre></details>}
      </section>
    </main>
  );
}
