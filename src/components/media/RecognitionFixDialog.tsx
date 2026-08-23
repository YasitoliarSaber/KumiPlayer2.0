import { Button } from '@fluentui/react-components';
import type { ImportPlanItem } from '../../api/types';
import type { MediaLibraryPreset } from '../../api/mediaPresets';

/**
 * P1-2（步骤 9）：统一人工处理弹窗（WinContentDialog 风格）。
 *
 * 两种模式：
 * 1. 单条修正（editingItem）：确认页「修正识别」→ 修正作品名/分组/季/集
 * 2. 批量 needs_review（needsReviewPreset）：卡片「处理识别结果」→
 *    为未识别单元逐个填作品名，生成可编辑 draft revision
 */
interface EditDraft {
  work_title: string;
  season_number: string;
  episode_number: string;
  group_type: string;
}

interface Props {
  /** 单条修正模式 */
  editingItem: ImportPlanItem | null;
  editDraft: EditDraft;
  onEditDraftChange: (draft: EditDraft) => void;
  onSaveItem: () => void;
  onCloseEditing: () => void;
  /** 批量 needs_review 模式 */
  needsReviewPreset: MediaLibraryPreset | null;
  needsReviewTitles: Record<string, string>;
  onNeedsReviewTitleChange: (unitId: string, title: string) => void;
  resolvingReviewUnitId: string;
  onResolveNeedsReview: (preset: MediaLibraryPreset, unitId: string) => void;
  onCloseNeedsReview: () => void;
  getPresetDisplayName: (preset: MediaLibraryPreset) => string;
  formatOpenlistSize: (bytes: number | null) => string;
}

export default function RecognitionFixDialog({
  editingItem,
  editDraft,
  onEditDraftChange,
  onSaveItem,
  onCloseEditing,
  needsReviewPreset,
  needsReviewTitles,
  onNeedsReviewTitleChange,
  resolvingReviewUnitId,
  onResolveNeedsReview,
  onCloseNeedsReview,
  getPresetDisplayName,
  formatOpenlistSize,
}: Props) {
  // 单条修正模式
  if (editingItem) {
    return (
      <div className="media-edit-backdrop" role="presentation">
        <section className="media-edit-dialog" role="dialog" aria-modal="true" aria-label="修正导入条目">
          <header>
            <h2>处理识别结果</h2>
            <Button appearance="subtle" onClick={onCloseEditing}>关闭</Button>
          </header>
          <div className="media-edit-source">
            <span className="media-edit-source-label">正在处理的文件</span>
            <strong className="media-edit-source-name">{editingItem.relative_path.split('/').pop() || editingItem.relative_path}</strong>
            <code className="media-edit-source-path" title={editingItem.relative_path}>{editingItem.relative_path}</code>
            {editingItem.source_size ? <span className="media-edit-source-size">大小 {formatOpenlistSize(editingItem.source_size)}</span> : null}
          </div>
          <label>作品名称
            <input value={editDraft.work_title} onChange={(event) => onEditDraftChange({ ...editDraft, work_title: event.target.value })} />
          </label>
          <div className="media-edit-grid">
            <label>分组
              <select value={editDraft.group_type} onChange={(event) => onEditDraftChange({ ...editDraft, group_type: event.target.value })}>
                <option value="season">季度</option>
                <option value="special">特别篇</option>
                <option value="movie">电影</option>
                <option value="ignored">忽略</option>
              </select>
            </label>
            <label>季度
              <input type="number" value={editDraft.season_number} onChange={(event) => onEditDraftChange({ ...editDraft, season_number: event.target.value })} />
            </label>
            <label>集数
              <input type="number" value={editDraft.episode_number} onChange={(event) => onEditDraftChange({ ...editDraft, episode_number: event.target.value })} />
            </label>
          </div>
          <footer>
            <Button appearance="secondary" onClick={onCloseEditing}>取消</Button>
            <Button appearance="primary" onClick={() => void onSaveItem()}>保存处理结果</Button>
          </footer>
        </section>
      </div>
    );
  }

  // 批量 needs_review 模式
  if (needsReviewPreset) {
    return (
      <div className="media-edit-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onCloseNeedsReview(); }}>
        <section className="media-edit-dialog media-needs-review-dialog" role="dialog" aria-modal="true" aria-label="处理识别结果">
          <header>
            <div>
              <span className="media-import-step-label">人工处理</span>
              <h2>{getPresetDisplayName(needsReviewPreset)}</h2>
              <p>以下识别单元未能自动确认作品身份，请填写作品名称后生成可编辑版本，再进入确认页核对。</p>
            </div>
            <Button appearance="subtle" onClick={onCloseNeedsReview}>关闭</Button>
          </header>
          <div className="media-needs-review-list">
            {(needsReviewPreset.openlist_needs_review_units ?? []).map((unit) => (
              <div key={unit.unit_id} className="media-needs-review-unit">
                <label className="media-needs-review-boundary" title={unit.boundary}>{unit.boundary || unit.work_key}</label>
                <input
                  value={needsReviewTitles[unit.unit_id] ?? ''}
                  placeholder="填写作品名称"
                  onChange={(event) => onNeedsReviewTitleChange(unit.unit_id, event.target.value)}
                  disabled={Boolean(resolvingReviewUnitId)}
                />
                <Button appearance="primary" disabled={Boolean(resolvingReviewUnitId)} onClick={() => void onResolveNeedsReview(needsReviewPreset, unit.unit_id)}>{resolvingReviewUnitId === unit.unit_id ? '处理中…' : '生成可编辑版本'}</Button>
              </div>
            ))}
          </div>
          <footer><Button appearance="secondary" onClick={onCloseNeedsReview}>取消</Button></footer>
        </section>
      </div>
    );
  }

  return null;
}
