import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
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

  const sourceWorks = source === 'all' ? works : works.filter((work) => work.source === source);
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

function recentEpisodeLabel(item: PlaybackHistoryItem) {
  const episodeNumber = Number(item.episode_number || 0);
  const seasonNumber = Number(item.season_number || 0);
  if (episodeNumber > 0) {
    const seasonPrefix = seasonNumber > 0 ? `第 ${seasonNumber} 季 · ` : '';
    return `${seasonPrefix}最近播放第 ${episodeNumber} 集`;
  }
  return item.episode_title ? `最近播放：${item.episode_title}` : '最近播放';
}
