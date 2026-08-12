import { useMemo } from 'react';
import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
import VirtualizedPosterGrid from '../components/library/VirtualizedPosterGrid';
import LoadingState from '../components/ui/loading-state';

export default function SearchPage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const { query, source, posterSize } = useUiStore();
  const keyword = query.trim().toLowerCase();
  const columnsPerRow = normalizeColumns(posterSize);

  const results = useMemo(() => {
    if (!keyword) return [];
    const recentRank = new Map(history.map((item, index) => [item.work_id, index]));
    return works
      .filter((work) => {
        if (source !== 'all' && work.source !== source) return false;
        const haystack = [
          work.title,
          work.original_title,
          ...(work.related_works || []).map((item: any) => item.title),
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return haystack.includes(keyword);
      })
      .sort((a, b) => {
        const aRank = recentRank.get(a.work_id) ?? Number.MAX_SAFE_INTEGER;
        const bRank = recentRank.get(b.work_id) ?? Number.MAX_SAFE_INTEGER;
        if (aRank !== bRank) return aRank - bRank;
        return (b.rating || 0) - (a.rating || 0);
      });
  }, [history, keyword, source, works]);

  if (loading && works.length === 0) return <LoadingState label="正在准备搜索" detail="正在读取媒体索引" />;
  if (error && works.length === 0) return <CenteredMessage>{error}</CenteredMessage>;

  return (
    <div className="category-page">
      <div className="category-head">
        <div className="category-title-block">
          <h1>搜索</h1>
          <span>找到 {results.length} 部</span>
        </div>
      </div>

      {results.length === 0 ? (
        <CenteredMessage>没有找到相关作品</CenteredMessage>
      ) : (
        <div className="category-grid-wrap">
          <VirtualizedPosterGrid works={results} columns={columnsPerRow} />
        </div>
      )}
    </div>
  );
}

function normalizeColumns(value: number) {
  if (value >= 3 && value <= 8) return Math.round(value);
  if (value >= 240) return 3;
  if (value >= 210) return 4;
  if (value >= 170) return 5;
  return 6;
}

function CenteredMessage({ children }: { children: string }) {
  return (
    <div className="page-loading-wrap">
      <div className="page-loading-message">{children}</div>
    </div>
  );
}
