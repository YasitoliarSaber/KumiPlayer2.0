import { useState } from 'react';
import LibraryViewControls, { normalizeColumns } from '../components/library/LibraryViewControls';
import VirtualizedPosterGrid from '../components/library/VirtualizedPosterGrid';
import type { WorkIndex } from '../api/types';
import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';

export default function FavoritesPage() {
  const works = useLibraryStore((state) => state.works);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const { goHome, source, posterSize } = useUiStore();
  const [columnCapacity, setColumnCapacity] = useState<number>();

  if (loading && works.length === 0) {
    return <PageState message="加载中..." />;
  }

  if (error && works.length === 0) {
    return <PageState message={error} />;
  }

  const sourceWorks = source === 'all' ? works : works.filter((work) => work.source === source);
  const favoriteWorks = selectFavoriteWorks(sourceWorks);
  const columnsPerRow = normalizeColumns(posterSize);

  return (
    <div className="recent-page favorites-page">
      <div className="favorites-head">
        <header className="page-title-block">
          <h1>我的收藏</h1>
          <p>已收藏 {favoriteWorks.length} 部</p>
        </header>
        <div className="category-toolbar library-view-toolbar" role="toolbar" aria-label="收藏视图工具">
          <LibraryViewControls maxColumns={columnCapacity} />
        </div>
      </div>

      {favoriteWorks.length === 0 ? (
        <div className="empty-state empty-state-recent">
          <div className="empty-state-kicker">我的收藏</div>
          <div className="empty-state-title">还没有收藏作品</div>
          <div className="empty-state-subtext">在作品详情页点击心形按钮，即可把作品保存到这里。</div>
          <button className="empty-state-action" onClick={goHome}>回到首页</button>
        </div>
      ) : (
        <div className="category-grid-wrap favorites-grid-wrap">
          <VirtualizedPosterGrid
            works={favoriteWorks}
            columns={columnsPerRow}
            recentLabel="已收藏"
            onColumnCapacityChange={setColumnCapacity}
          />
        </div>
      )}
    </div>
  );
}

function PageState({ message }: { message: string }) {
  return <div className="page-loading-wrap"><div className="page-loading-message">{message}</div></div>;
}

export function selectFavoriteWorks(works: WorkIndex[]) {
  return works
    .filter((work) => Boolean(work.watch_status?.favorite))
    .sort((left, right) => {
      const rightUpdatedAt = Date.parse(right.watch_status?.updated_at || '') || 0;
      const leftUpdatedAt = Date.parse(left.watch_status?.updated_at || '') || 0;
      return rightUpdatedAt - leftUpdatedAt || left.title.localeCompare(right.title, 'zh-Hans-CN');
    });
}
