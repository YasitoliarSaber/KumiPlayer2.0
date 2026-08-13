import { ProgressBar, Spinner } from '@fluentui/react-components';
import { CheckCircle2, CircleAlert, Layers3, LoaderCircle, Sparkles } from 'lucide-react';
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
  updating_library: '正在更新媒体库',
  completed: '已完成',
  needs_review: '待处理识别结果',
  failed: '处理失败',
  cancelled: '已停止',
};

const stageLabels = ['识别作品', '生成媒体库', '补充资料', '更新媒体库'];

type StageState = 'waiting' | 'active' | 'complete' | 'attention';

function isActive(unit: BackgroundImportUnit): boolean {
  return ['queued', 'discovering', 'mirroring', 'scraping', 'updating_library'].includes(unit.state);
}

function unitDetail(unit: BackgroundImportUnit): string {
  if (unit.error) return unit.error;
  const job = unit.library_rebuild_job || unit.scrape_job || unit.mirror_job;
  if (job?.message) return job.message;
  if (unit.state === 'needs_review') return '已保留该作品，稍后可在媒体库维护中处理。';
  return unit.video_count ? `${unit.video_count} 个视频` : '正在整理任务状态';
}

function jobState(job: BackgroundImportUnit['mirror_job']): 'waiting' | 'active' | 'complete' | 'attention' {
  if (!job) return 'waiting';
  if (job.status === 'succeeded') return 'complete';
  if (job.status === 'failed' || job.status === 'cancelled') return 'attention';
  return 'active';
}

function stageState(
  index: number,
  roots: OpenListImportBatch['roots'],
  units: BackgroundImportUnit[],
): StageState {
  const discovery = roots.map((root) => root.job_status || root.status);
  if (index === 0) {
    if (discovery.some((status) => ['pending', 'queued', 'running'].includes(status))) return 'active';
    return 'complete';
  }
  const jobs = units.map((unit) => index === 1
    ? unit.mirror_job
    : index === 2
      ? unit.scrape_job
      : unit.library_rebuild_job);
  const states = jobs.map(jobState);
  if (states.some((state) => state === 'active')) return 'active';
  if (states.some((state) => state === 'complete')) return 'complete';
  if (units.some((unit) => unit.state === 'completed')) return 'complete';
  if (index === 1 && units.some((unit) => ['mirrored', 'scraping'].includes(unit.state))) return 'complete';
  if (index === 2 && units.some((unit) => unit.state === 'scraping')) return 'active';
  if (states.some((state) => state === 'attention') || units.some((unit) => unit.state === 'failed')) return 'attention';
  if (units.some((unit) => unit.state === 'needs_review')) return 'waiting';
  return index === 1 && units.some((unit) => unit.state === 'mirroring') ? 'active' : 'waiting';
}

function currentStage(states: StageState[]): number {
  const active = states.findIndex((state) => state === 'active');
  if (active >= 0) return active;
  const attention = states.findIndex((state) => state === 'attention');
  if (attention >= 0) return attention;
  const waiting = states.findIndex((state) => state === 'waiting');
  return waiting >= 0 ? waiting : states.length - 1;
}

function stageTitle(index: number, state: StageState): string {
  if (state === 'attention') return index === 0 ? '识别结果需要处理' : '部分结果需要处理';
  if (state === 'active') return stageLabels[index];
  if (state === 'complete') return index === 3 ? '媒体库已更新' : `${stageLabels[index]}完成`;
  return stageLabels[index];
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
  const stages = stageLabels.map((_, index) => stageState(index, roots, units));
  const current = currentStage(stages);
  const overallStage = stages[current];
  const overallTitle = stageTitle(current, overallStage);
  const activeJob = units.flatMap((unit) => [unit.library_rebuild_job, unit.scrape_job, unit.mirror_job])
    .find((job) => job && ['queued', 'running'].includes(job.status));
  const summary = activeJob?.message
    || (overallStage === 'complete' ? '所有已确认作品均已处理完毕。' : attention ? `${attention} 个项目需要处理，其他作品继续执行。` : '正在读取后台任务状态。');

  return (
    <section className="media-background-import media-stage-shell" aria-label="后台导入状态">
      <header className="media-background-header">
        <div className="media-background-icon"><LoaderCircle size={22} /></div>
        <div>
          <span>后台导入</span>
          <h2>{overallTitle}</h2>
          <p>{sourceLabel}已提交 · {summary}</p>
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
      <ol className="media-workbench-substages media-background-stages" aria-label="执行阶段">
        {stageLabels.map((label, index) => {
          const state = stages[index];
          return <li
            key={label}
            className={state === 'complete' ? 'complete' : state === 'active' ? 'active' : state === 'attention' ? 'attention' : ''}
            aria-current={index === current ? 'step' : undefined}
          >
            <span>{state === 'complete' ? <CheckCircle2 size={15} /> : index + 1}</span>
            <span>{label}</span>
          </li>;
        })}
      </ol>
      <section className="media-background-list" aria-label="作品处理列表">
        {units.length ? units.map((unit) => (
          <article key={`${unit.unit_id}-${unit.revision_id}`} className={`media-background-unit ${unit.state}`}>
            <div className="media-background-unit-icon">{isActive(unit) ? <Spinner size="tiny" /> : unit.state === 'completed' ? <CheckCircle2 size={17} /> : unit.state === 'mirrored' ? <Layers3 size={17} /> : unit.state === 'scraping' ? <Sparkles size={17} /> : <CircleAlert size={17} />}</div>
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
