import { ProgressBar, Spinner } from '@fluentui/react-components';
import { CheckCircle2, CircleAlert, Clock3, LoaderCircle } from 'lucide-react';
import type { BackgroundImportUnit, OpenListImportBatch } from '../../api/openlist';

type Props = {
  batch: OpenListImportBatch | null;
  source: 'local' | 'openlist';
};

const stateLabel: Record<string, string> = {
  queued: '等待处理',
  discovering: '正在识别',
  mirroring: '正在生成媒体库',
  mirrored: '媒体库已生成',
  scraping: '正在补充资料',
  completed: '已完成',
  needs_review: '待处理识别结果',
  failed: '处理失败',
  cancelled: '已停止',
};

function isActive(unit: BackgroundImportUnit): boolean {
  return ['queued', 'discovering', 'mirroring', 'scraping'].includes(unit.state);
}

function unitDetail(unit: BackgroundImportUnit): string {
  if (unit.error) return unit.error;
  const job = unit.scrape_job || unit.mirror_job;
  if (job?.message) return job.message;
  if (unit.state === 'needs_review') return '已保留该作品，稍后可在媒体库维护中处理。';
  return unit.video_count ? `${unit.video_count} 个视频` : '正在整理任务状态';
}

export default function MediaBackgroundImportStatus({ batch, source }: Props) {
  const roots = batch?.roots || [];
  const units = roots.flatMap((root) => root.units || []);
  const active = units.filter(isActive).length + roots.filter((root) => ['queued', 'running', 'pending'].includes(root.job_status || root.status)).length;
  const completed = units.filter((unit) => unit.state === 'completed').length;
  const attention = units.filter((unit) => ['needs_review', 'failed', 'cancelled'].includes(unit.state)).length;
  const total = Math.max(units.length, roots.length, 1);
  const progress = Math.min(1, (completed + attention) / total);
  const sourceLabel = source === 'local' ? '本地路径' : 'OpenList 网盘路径';

  return (
    <section className="media-background-import media-stage-shell" aria-label="后台导入状态">
      <header className="media-background-header">
        <div className="media-background-icon"><LoaderCircle size={22} /></div>
        <div>
          <span>后台导入</span>
          <h2>后台处理中</h2>
          <p>{sourceLabel}已提交。你可以离开此页，任务会持续执行并在返回时恢复状态。</p>
        </div>
        <div className={`media-background-state ${active ? 'active' : attention ? 'attention' : 'complete'}`}>
          {active ? <Spinner size="tiny" /> : attention ? <CircleAlert size={16} /> : <CheckCircle2 size={16} />}
          <span>{active ? '正在处理' : attention ? '部分项目需要处理' : '处理完成'}</span>
        </div>
      </header>
      <div className="media-background-progress" aria-label="后台处理进度">
        <div><strong>{units.length ? `${completed} / ${units.length} 部作品已完成` : '正在建立作品清单'}</strong><span>{attention ? `${attention} 个项目不会阻塞其他作品` : '识别、媒体库生成与资料补充会自动衔接'}</span></div>
        <ProgressBar value={progress} max={1} />
      </div>
      <div className="media-background-stage-strip" aria-label="后台处理阶段">
        <span><Clock3 size={15} />识别作品</span><span>生成媒体库</span><span>补充资料</span><span>更新媒体库</span>
      </div>
      <section className="media-background-list" aria-label="作品处理列表">
        {units.length ? units.map((unit) => (
          <article key={`${unit.unit_id}-${unit.revision_id}`} className={`media-background-unit ${unit.state}`}>
            <div className="media-background-unit-icon">{isActive(unit) ? <Spinner size="tiny" /> : unit.state === 'completed' ? <CheckCircle2 size={17} /> : <CircleAlert size={17} />}</div>
            <div><strong>{unit.work_title}</strong><span>{unitDetail(unit)}</span></div>
            <span className="media-background-unit-state">{stateLabel[unit.state] || '正在处理'}</span>
          </article>
        )) : roots.map((root) => (
          <article key={root.root_id} className="media-background-unit discovering">
            <div className="media-background-unit-icon"><Spinner size="tiny" /></div>
            <div><strong>{root.remote_locator}</strong><span>{root.message || '正在读取目录并识别其中的作品。'}</span></div>
            <span className="media-background-unit-state">正在识别</span>
          </article>
        ))}
      </section>
    </section>
  );
}
