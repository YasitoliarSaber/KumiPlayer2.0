import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
import { matchesSourceFilter } from '../utils/sourceFilter';
import RecentWatchCard from '../components/library/RecentWatchCard';
import type { PlaybackHistoryItem, WorkIndex } from '../api/types';
import './RecentPage.css';

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
        {/* 「最近观看」是继续播放入口，不是播放历史日志：标题下只留一个弱化的项目数，
            不再写「最近 N 部」这种没有行动价值的信息。 */}
        {hasContent && <p className="recent-count">{recentWorks.length} 个项目</p>}
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
        <div className="recent-watch-grid">
          {recentWorks.map((item) => (
            <RecentWatchCard
              key={item.work.work_id}
              work={item.work}
              history={item.history}
              episodeLabel={item.work.media_type === 'movie' ? '电影' : recentEpisodeCode(item.history)}
              statusLabel={recentRemainingLabel(item.history)}
              timeLabel={recentTimeLabel(item.history.played_at || item.history.updated_at)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

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

/**
 * 「最近观看」卡片第二层的状态文案（规格：继续观看，不是播放日志）。
 *
 * - 已看完：`已看完 ✓`；
 * - 有总时长：`还剩 23 分钟` / `还剩 1 小时 14 分`（向上取整，宁可高估不多报）；
 * - 缺总时长或没有可用进度：`继续观看`。
 *
 * 刻意**不再**输出 `12:34 / 24:00 · 52%` 这类调试信息：精确位置、总时长与百分比
 * 不同时以文本展示，百分比由图片底部的进度条表达。
 */
export function recentRemainingLabel(item: PlaybackHistoryItem) {
  if (item.completed) return '已看完 ✓';
  const position = Number(item.position ?? 0);
  const duration = Number(item.duration ?? 0);
  if (!Number.isFinite(position) || !Number.isFinite(duration) || duration <= 0) return '继续观看';
  const remaining = duration - position;
  if (!Number.isFinite(remaining) || remaining <= 0) return '继续观看';
  if (remaining < 60) return '还剩不到 1 分钟';
  const minutes = Math.max(1, Math.ceil(remaining / 60));
  if (minutes < 60) return `还剩 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `还剩 ${hours} 小时` : `还剩 ${hours} 小时 ${rest} 分`;
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
 * 只显示作品名不算最近播放；缺集号/集标题时至少给出剩余进度与时间，绝不只留"最近播放"。
 * 该汇总文案用于卡片无障碍名称，与卡片三层结构保持一致（不含精确位置/总时长/百分比）。
 */
export function recentEpisodeLabel(item: PlaybackHistoryItem, now: Date = new Date()) {
  return [recentEpisodeCode(item), recentRemainingLabel(item), recentTimeLabel(item.played_at || item.updated_at, now)]
    .filter((part) => Boolean(part))
    .join(' · ');
}

/** 未知季号/集号保持未知；旧快照只解析明确编号，不推断成 S01E01。 */
export function recentEpisodeCode(item: PlaybackHistoryItem) {
  const season = explicitNumber(item.season_number, item.season_snapshot, /^(?:第\s*(\d+)\s*季|S(\d+))$/i, 0);
  const episode = explicitNumber(item.episode_number, item.episode_snapshot, /^(?:第\s*(\d+)\s*集|E(\d+))$/i, 1);
  if (episode !== null) {
    return `${season === null ? '' : `S${String(season).padStart(2, '0')}`}E${String(episode).padStart(2, '0')}`;
  }
  return item.episode_title?.trim() || item.episode_snapshot?.trim() || '集号待定';
}

function explicitNumber(value: number | null | undefined, snapshot: string | undefined, pattern: RegExp, minimum: number) {
  if (typeof value === 'number' && Number.isInteger(value) && value >= minimum) return value;
  const match = (snapshot || '').trim().match(pattern);
  if (!match) return null;
  const number = Number(match[1] || match[2]);
  return Number.isInteger(number) && number >= minimum ? number : null;
}
