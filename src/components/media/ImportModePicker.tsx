import { Button } from '@fluentui/react-components';
import { Cloud, FileText, HardDrive, Layers, type LucideIcon } from 'lucide-react';
import type { MediaWorkflowIngestMode, MediaWorkflowSource } from '../../stores/mediaWorkflow';

/**
 * P1-4：导入模式选择（WinSettingsCard 风格卡片）。
 *
 * 用户核心诉求（问题记录问题 3）：导入模式不清晰且易重复——
 * 用「导入模式」作为顶层概念，来源是模式的属性，而非平铺的来源按钮。
 */
export interface ImportModeOption {
  key: MediaWorkflowIngestMode;
  title: string;
  description: string;
  icon: LucideIcon;
  /** 选中该模式时建议的来源（local / pan115 / baidu / openlist） */
  source: MediaWorkflowSource;
  /** 推荐标记（模式 3：首次 TXT 全量 + 后续 OpenList 增量） */
  recommended?: boolean;
  /** 需要 OpenList 已配置才可选 */
  requiresOpenlist?: boolean;
}

export const IMPORT_MODES: ImportModeOption[] = [
  {
    key: 'local',
    title: '本地路径模式',
    description: '从本地目录或局域网目录导入到本地媒体库，适合硬盘已整理好的文件。',
    icon: HardDrive,
    source: 'local',
  },
  {
    key: 'tree',
    title: '目录树导入模式',
    description: '导入已导出的网盘目录树 TXT，无需配置 OpenList，适合一次性批量建库。',
    icon: FileText,
    source: 'pan115',
  },
  {
    key: 'tree_openlist',
    title: 'TXT 全量 + OpenList 增量',
    description: '首次用目录树 TXT 安全全量建库，后续用 OpenList 基于已有库做增量更新，避免重复建库。',
    icon: Layers,
    source: 'pan115',
    recommended: true,
    requiresOpenlist: true,
  },
  {
    key: 'openlist',
    title: '仅 OpenList 模式',
    description: '浏览 OpenList 远端目录，直接选择目录批量导入（来源网盘类型在设置页绑定）。',
    icon: Cloud,
    source: 'openlist',
    requiresOpenlist: true,
  },
];

interface Props {
  source: MediaWorkflowSource;
  /** 当前选中的导入模式 */
  ingestMode: MediaWorkflowIngestMode;
  onSetIngestMode: (mode: MediaWorkflowIngestMode) => void;
  /** OpenList 是否已配置（未配置时模式 3/4 置灰 + 引导） */
  openlistConfigured: boolean;
  onGoSettings: () => void;
}

export default function ImportModePicker({
  source,
  ingestMode,
  onSetIngestMode,
  openlistConfigured,
  onGoSettings,
}: Props) {
  const selectedMode = ingestMode || (source === 'local'
    ? 'local'
    : source === 'openlist'
      ? 'openlist'
      : source === 'pan115' || source === 'baidu'
        ? 'tree'
        : 'tree');

  return (
    <section className="media-import-mode-picker" aria-label="导入模式选择">
      <div className="media-import-mode-list">
        {IMPORT_MODES.map((mode) => {
          const Icon = mode.icon;
          const blocked = Boolean(mode.requiresOpenlist) && !openlistConfigured;
          const selected = selectedMode === mode.key;
          return (
            <button
              type="button"
              key={mode.key}
              className={`media-import-mode-card${selected ? ' selected' : ''}${blocked ? ' blocked' : ''}`}
              aria-pressed={selected}
              disabled={blocked}
              onClick={() => onSetIngestMode(mode.key)}
            >
              <span className="media-import-mode-icon" aria-hidden="true"><Icon size={20} /></span>
              <span className="media-import-mode-text">
                <strong>
                  {mode.title}
                  {mode.recommended && <em className="media-import-mode-recommended">推荐</em>}
                </strong>
                <small>{mode.description}</small>
              </span>
              {blocked
                ? <Button appearance="subtle" size="small" onClick={(event) => { event.stopPropagation(); onGoSettings(); }}>前往设置</Button>
                : <span className="media-import-mode-arrow" aria-hidden="true">›</span>}
            </button>
          );
        })}
      </div>
    </section>
  );
}
