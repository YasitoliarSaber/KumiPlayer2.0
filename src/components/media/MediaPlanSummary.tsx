import type { ImportPreview, PreviewGroup } from '../../api/types';

type CategoryRow = {
  label: string;
  workCount: number;
  itemCount: number;
  groups: PreviewGroup[];
  unit: 'episode' | 'video';
};

function fallbackWorkKey(group: PreviewGroup): string {
  if (group.work_id) return `id:${group.work_id}`;
  return `fallback:${group.card_type || ''}|${group.series_group || group.work_title || ''}|${group.year ?? ''}`;
}

function distinctWorkCount(groups: PreviewGroup[]): number {
  return new Set(groups.map(fallbackWorkKey)).size;
}

function groupCount(groups: PreviewGroup[]): number {
  return groups.reduce((sum, group) => sum + (group.item_count || 0), 0);
}

function buildCategory(label: string, groups: PreviewGroup[], unit: 'episode' | 'video'): CategoryRow {
  return { label, groups, workCount: distinctWorkCount(groups), itemCount: groupCount(groups), unit };
}

function groupDetailLabel(group: PreviewGroup): string {
  const title = group.work_title || group.series_group || '未命名作品';
  if (group.group_type === 'season') {
    return `${title} · 第 ${group.season_number ?? '?'} 季`;
  }
  if (group.group_type === 'special' || group.group_type === 'sps') {
    const number = group.season_number != null && group.season_number !== 0 ? ` ${group.season_number}` : '';
    return `${title} · 特别篇${number}`;
  }
  return title;
}

const KNOWN_GROUPS = ['season', 'movie', 'special', 'sps', 'op_ed', 'auxiliary'];

/**
 * 确认页默认可见的媒体摘要：用真实 work_count / groups 回答
 * “识别了哪些媒体、是否存在会影响确认的异常”，不伪造作品、季集或海报。
 */
export default function MediaPlanSummary({ preview }: { preview: ImportPreview }) {
  const summary = preview.summary;
  const workCount = summary.work_count ?? 0;
  const videoCount = summary.video_count ?? 0;
  const needsReviewCount = summary.needs_review_count ?? 0;
  const mainSeries = preview.groups.filter((group) => group.group_type === 'season');
  const movies = preview.groups.filter((group) => group.group_type === 'movie');
  const specials = preview.groups.filter((group) => group.group_type === 'special' || group.group_type === 'sps');
  const auxiliary = preview.groups.filter((group) => group.group_type === 'op_ed' || group.group_type === 'auxiliary');
  const other = preview.groups.filter((group) => !KNOWN_GROUPS.includes(group.group_type));

  const categories: CategoryRow[] = [
    buildCategory('主系列', mainSeries, 'episode'),
    buildCategory('电影', movies, 'video'),
    buildCategory('特别篇', specials, 'video'),
    buildCategory('辅助视频', auxiliary, 'video'),
    buildCategory('其他已识别视频', other, 'video'),
  ];

  const signals: Array<[number, string]> = [];
  if ((summary.low_confidence_count ?? 0) > 0) signals.push([summary.low_confidence_count, '低置信']);
  if ((summary.ungrouped_video_count ?? 0) > 0) signals.push([summary.ungrouped_video_count, '未分组']);
  if ((summary.duplicate_episode_count ?? 0) > 0) signals.push([summary.duplicate_episode_count, '重复剧集']);

  return (
    <section className="media-media-summary" aria-labelledby="media-summary-title">
      <div className="media-media-summary-heading">
        <div>
          <span className="media-import-step-label">识别结果</span>
          <h3 id="media-summary-title">已识别的媒体</h3>
        </div>
        {preview.groups.length > 0 && (
          <details className="media-inline-details media-summary-details">
            <summary>查看完整列表</summary>
            <ul className="media-summary-detail-list">
              {categories.filter((row) => row.groups.length > 0).flatMap((row) =>
                row.groups.map((group) => (
                  <li key={`${group.work_id}-${group.card_type}-${group.group_type}-${group.season_number ?? ''}`}>
                    <span className="media-summary-detail-kind">{row.label}</span>
                    <strong>{groupDetailLabel(group)}</strong>
                    <span>{group.item_count} {row.unit === 'episode' ? '集' : '个视频'}</span>
                  </li>
                )),
              )}
            </ul>
          </details>
        )}
      </div>

      <div className="media-media-summary-counts">
        <div className="media-media-summary-row">
          <span>主系列</span>
          <strong>{workCount}</strong>
        </div>
        <div className="media-media-summary-row">
          <span>视频</span>
          <strong>{videoCount}</strong>
        </div>
        <div className={`media-media-summary-row${needsReviewCount > 0 ? ' attention' : ''}`}>
          <span>需处理</span>
          <strong>{needsReviewCount}</strong>
        </div>
      </div>

      <div className="media-media-summary-categories">
        {categories.map((row) => (
          row.groups.length === 0 ? null : (
            <div className="media-media-summary-row" key={row.label}>
              <span>{row.label}</span>
              <strong>
                {row.label === '辅助视频' || row.label === '其他已识别视频'
                  ? `${row.itemCount} 个视频`
                  : `${row.workCount} 部 · ${row.itemCount} ${row.unit === 'episode' ? '集' : '个视频'}`}
              </strong>
            </div>
          )
        ))}
      </div>

      {signals.length > 0 && (
        <div className="media-media-summary-signals" role="status">
          {signals.map(([count, label]) => <span key={label}>{label} {count}</span>)}
        </div>
      )}
    </section>
  );
}
