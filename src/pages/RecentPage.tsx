import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
import { matchesSourceFilter } from '../utils/sourceFilter';
import PosterCard from '../components/library/PosterCard';
import type { PlaybackHistoryItem, WorkIndex } from '../api/types';

export default function RecentPage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const { goHome, source } = useUiStore();

  if (loading && works.length === 0) {
    return (
      <div className="page-loading-wrap">
        <div className="page-loading-message">加载中...</div>
      </div>
    );
  }

  if (error && works.length === 0) {
    return (
      <div className="page-loading-wrap">
        <div className="page-loading-message">{error}</div>
      </div>
    );
  }

  const sourceWorks = works.filter((work) => matchesSourceFilter(work, source));
  const recentWorks = selectRecentWorks(sourceWorks, history);
  const hasContent = recentWorks.length > 0;

  return (
    <div className="recent-page">
      <header className="page-title-block">
        <h1>最近观看</h1>
        <p>
          最近 {recentWorks.length} 部
        </p>
      </header>

      {!hasContent ? (
        <div className="empty-state empty-state-recent">
          <div className="empty-state-kicker">观看记录</div>
          <div className="empty-state-title">还没有最近观看记录</div>
          <div className="empty-state-subtext">
            开始播放作品后，这里会保留最近或高频打开的内容
          </div>
          <button className="empty-state-action" onClick={goHome}>回到首页</button>
        </div>
      ) : (
        <div className="recent-card-grid">
          {recentWorks.slice(0, RECENT_SECTION_LIMIT).map((item) => (
            <PosterCard
              key={item.work.work_id}
              work={item.work}
              showType="recent"
              recentLabel={recentEpisodeLabel(item.history)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

const RECENT_SECTION_LIMIT = 6;
export function selectRecentWorks(works: WorkIndex[], history: PlaybackHistoryItem[]) {
  const workById = new Map(works.map((work) => [work.work_id, work]));
  const candidates = new Map<string, { work: WorkIndex; history: PlaybackHistoryItem; firstIndex: number; playCount: number }>();

  history.forEach((item, index) => {
    const work = workById.get(item.work_id);
    if (!work) return;
    const existing = candidates.get(item.work_id);
    if (existing) {
      existing.playCount += 1;
      return;
    }
    candidates.set(item.work_id, { work, history: item, firstIndex: index, playCount: 1 });
  });

  return [...candidates.values()].sort((left, right) => {
    const scoreDelta = recentViewingPriority(right) - recentViewingPriority(left);
    return scoreDelta || left.firstIndex - right.firstIndex;
  });
}

function recentViewingPriority(
  item: { firstIndex: number; playCount: number },
) {
  const recency = 12 / (1 + item.firstIndex);
  const frequency = Math.min(item.playCount, 4) * 4;
  return recency + frequency;
}

function formatClock(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`;
}

/** 播放进度文案：`12:34 / 24:00 · 52%`；看完显示"已看完"。 */
export function recentProgressLabel(item: PlaybackHistoryItem) {
  if (item.completed) return '已看完';
  const position = Number(item.position ?? 0);
  if (!Number.isFinite(position) || position <= 0) return '';
  const duration = Number(item.duration ?? 0);
  if (!Number.isFinite(duration) || duration <= 0) return `看到 ${formatClock(position)}`;
  const percent = Math.min(100, Math.round((position / duration) * 100));
  return `${formatClock(position)} / ${formatClock(duration)} · ${percent}%`;
}

/** 最近播放时间：今天 / 昨天 / 月日 + 时刻。 */
export function recentTimeLabel(value: string, now: Date = new Date()) {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return '';
  const clock = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
  if (at.toDateString() === now.toDateString()) return `今天 ${clock}`;
  const yesterday = new Date(now.getTime() - 86_400_000);
  if (at.toDateString() === yesterday.toDateString()) return `昨天 ${clock}`;
  return `${at.getMonth() + 1} 月 ${at.getDate()} 日 ${clock}`;
}

/**
 * 「最近播放」必须回答：**看了哪一部、哪一集、看到哪、什么时候**（规格 §12）。
 * 只显示作品名不算最近播放；缺集号/集标题时至少给出进度与时间，绝不只留"最近播放"。
 */
export function recentEpisodeLabel(item: PlaybackHistoryItem, now: Date = new Date()) {
  const episodeNumber = Number(item.episode_number ?? 0);
  const seasonNumber = Number(item.season_number ?? 0);
  // 后端 `/history` 返回的是**快照**字段（`episode_snapshot`/`season_snapshot`）；
  // 数字集号只有详情页补充时才存在。此前只读数字，导致永远退化成"最近播放"。
  const episodeSnapshot = (item.episode_snapshot || '').trim();
  const seasonSnapshot = (item.season_snapshot || '').trim();
  const snapshotLabel = [seasonSnapshot, episodeSnapshot].filter(Boolean).join(' · ');
  const base = episodeNumber > 0
    ? `${seasonNumber > 0 ? `第 ${seasonNumber} 季 · ` : ''}最近播放第 ${episodeNumber} 集`
    : snapshotLabel
      ? `最近播放：${snapshotLabel}`
      : item.episode_title ? `最近播放：${item.episode_title}` : '最近播放';
  return [base, recentProgressLabel(item), recentTimeLabel(item.updated_at, now)]
    .filter((part) => Boolean(part))
    .join(' · ');
}
