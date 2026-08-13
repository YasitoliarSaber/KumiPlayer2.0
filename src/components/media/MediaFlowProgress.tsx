import {
  Check,
  ClipboardCheck,
  FolderOpen,
  Layers3,
  LoaderCircle,
  type LucideIcon,
} from 'lucide-react';
import type { MediaWorkflowStep } from '../../stores/mediaWorkflow';

type FlowStep = Exclude<MediaWorkflowStep, 'maintenance'>;

const steps: Array<{ key: FlowStep; label: string; icon: LucideIcon }> = [
  { key: 'import', label: '导入媒体', icon: FolderOpen },
  { key: 'confirm', label: '确认计划', icon: ClipboardCheck },
  { key: 'workbench', label: '创建媒体库并补充资料', icon: Layers3 },
];

const backgroundSteps: Array<{ key: FlowStep; label: string; icon: LucideIcon }> = [
  { key: 'import', label: '导入媒体', icon: FolderOpen },
  { key: 'background', label: '后台处理', icon: LoaderCircle },
];

type Props = {
  step: MediaWorkflowStep;
  completedThrough: number;
  canEnter: (step: FlowStep) => boolean;
  onStepChange: (step: FlowStep) => void;
};

export default function MediaFlowProgress({
  step,
  completedThrough,
  canEnter,
  onStepChange,
}: Props) {
  const visibleSteps = step === 'background' ? backgroundSteps : steps;
  return (
    <nav className="media-workflow-progress" aria-label="媒体导入进度">
      <ol>
        {visibleSteps.map((item, index) => {
          const Icon = item.icon;
          const current = step === item.key;
          const complete = step !== 'maintenance' && index < completedThrough;
          return (
            <li key={item.key}>
              <button
                type="button"
                className={`media-workflow-step${current ? ' current' : ''}${complete ? ' complete' : ''}`}
                aria-current={current ? 'step' : undefined}
                disabled={!canEnter(item.key)}
                onClick={() => onStepChange(item.key)}
              >
                <span className="media-workflow-step-icon">
                  {complete ? <Check size={15} strokeWidth={2.4} /> : <Icon size={16} />}
                </span>
                <span>{item.label}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
