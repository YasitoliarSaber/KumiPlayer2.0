import type { ImportPreview, TaskRecord } from '../../api/types';
import type { BackgroundImportUnit, OpenListImportBatch } from '../../api/openlist';
import type { BackgroundImportSession } from '../../stores/mediaWorkflow';
import type { MediaLog } from './MediaLogList';
import MediaBackgroundImportStatus from './MediaBackgroundImportStatus';
import MediaTaskWorkbench from './MediaTaskWorkbench';

/**
 * P1-2（步骤 6）：统一任务面板。
 *
 * 用户确认（问题 4.4）：对用户只暴露「刮削进度」一个概念，
 * durable/legacy 分流对用户不可见，标题统一为「刮削进度」。
 */
interface Props {
  step: 'workbench' | 'background';
  backgroundImport: BackgroundImportSession | null;
  backgroundBatch: OpenListImportBatch | null;
  preview: ImportPreview | null;
  task: TaskRecord | null;
  taskKind: 'mirror' | 'scrape' | null;
  taskLogs: MediaLog[];
  /** 是否为刮削任务（scrape 类） */
  isScrapeTask: (task: TaskRecord | null) => boolean;
  onStart: (kind: 'mirror' | 'scrape') => void;
  onNewImport: () => void;
  onCancel: (() => void) | undefined;
  onReviewUnit: (unit: BackgroundImportUnit) => void;
  onRetryUnit: (unit: BackgroundImportUnit) => void;
  retryingUnitId: string;
  /** legacy 工作台开始按钮是否禁用 */
  workbenchDisabled: boolean;
}

export default function TaskDashboard({
  step,
  backgroundImport,
  backgroundBatch,
  preview,
  task,
  taskKind,
  taskLogs,
  isScrapeTask,
  onStart,
  onNewImport,
  onCancel,
  onReviewUnit,
  onRetryUnit,
  retryingUnitId,
  workbenchDisabled,
}: Props) {
  // durable TXT 批次（pan115/baidu）→ 作品级进度列表；其余 → 工作台
  const isDurableBatch = (backgroundImport?.source === 'pan115' || backgroundImport?.source === 'baidu')
    && Boolean(backgroundBatch);

  if (step === 'background') {
    return (
      <MediaBackgroundImportStatus
        batch={backgroundBatch}
        source={backgroundImport?.source || 'local'}
        title="刮削进度"
        onReviewUnit={onReviewUnit}
        onRetryUnit={(backgroundImport?.source === 'openlist' || backgroundImport?.source === 'local') ? onRetryUnit : undefined}
        retryingUnitId={retryingUnitId}
      />
    );
  }

  if (isDurableBatch) {
    return (
      <MediaBackgroundImportStatus
        batch={backgroundBatch}
        source={backgroundImport!.source}
        title="刮削进度"
        onReviewUnit={onReviewUnit}
        retryingUnitId={retryingUnitId}
      />
    );
  }

  if (!preview && !isScrapeTask(task)) return null;

  const scrapeMode = isScrapeTask(task) && taskKind === 'scrape';

  return (
    <MediaTaskWorkbench
      mode={scrapeMode ? 'scrape' : 'mirror'}
      title="刮削进度"
      description="根据确认内容生成媒体库并自动补充资料。开始前最多抽样验证 3 个代表视频，镜像完整后自动开始刮削。"
      task={task}
      logs={taskLogs}
      onStart={() => onStart(scrapeMode ? 'scrape' : 'mirror')}
      onNewImport={onNewImport}
      onCancel={onCancel}
      startLabel={scrapeMode ? '开始补充资料' : '创建媒体库'}
      disabled={workbenchDisabled}
    />
  );
}
