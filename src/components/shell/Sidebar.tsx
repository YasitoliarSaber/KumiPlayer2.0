import {
  Clapperboard,
  Clock3,
  Film,
  Heart,
  CalendarSync,
  Home,
  MonitorPlay,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  SlidersHorizontal,
  Tv,
  type LucideIcon,
} from 'lucide-react';
import { useUiStore, type LibraryView } from '../../stores/ui';
import { useLibraryStore } from '../../stores/library';
import { useBangumiStore } from '../../stores/bangumi';
import { buildBangumiImageUrl } from '../../api/bangumi';
import { categoryWorkCounts } from '../../utils/libraryCategories';
import DecodedImage from '../ui/DecodedImage';

type IconName = 'home' | 'favorites' | 'recent' | 'seasonal' | 'anime' | 'animeMovie' | 'series' | 'movie' | 'manage' | 'settings';

const primaryNavItems: { key: string; label: string; icon: IconName; page: string; category?: LibraryView }[] = [
  { key: 'home', label: '首页', icon: 'home', page: 'home' },
  { key: 'favorites', label: '我的收藏', icon: 'favorites', page: 'favorites' },
  { key: 'recent', label: '最近观看', icon: 'recent', page: 'recent' },
];

const libraryNavItems: { key: string; label: string; icon: IconName; page: string; category?: LibraryView }[] = [
  { key: 'seasonal', label: '新番', icon: 'seasonal', page: 'category', category: 'seasonal' },
  { key: 'anime_series', label: '番剧', icon: 'anime', page: 'category', category: 'anime_series' },
  { key: 'anime_movie', label: '动画电影', icon: 'animeMovie', page: 'category', category: 'anime_movie' },
  { key: 'live_series', label: '剧集', icon: 'series', page: 'category', category: 'live_series' },
  { key: 'live_movie', label: '电影', icon: 'movie', page: 'category', category: 'live_movie' },
];

export default function Sidebar() {
  const {
    page,
    activeCategory,
    sidebarMode,
    goHome,
    goFavorites,
    goRecent,
    goCategory,
    goManage,
    goSettings,
    setQuery,
    source,
    toggleSidebar,
  } = useUiStore();
  const works = useLibraryStore((state) => state.works);
  const { user, hasStoredCredential } = useBangumiStore();
  const categoryCounts = categoryWorkCounts(works, source);
  const sidebarExpanded = sidebarMode === 'expanded';
  const sidebarHidden = sidebarMode === 'hidden';

  const isActive = (item: typeof primaryNavItems[0]) => {
    if (item.page === 'category') {
      return (page === 'category' || page === 'detail') && activeCategory === item.category;
    }
    return page === item.page;
  };

  const handleClick = (item: typeof primaryNavItems[0]) => {
    setQuery('');
    if (item.page === 'home') goHome();
    else if (item.page === 'favorites') goFavorites();
    else if (item.page === 'recent') goRecent();
    else if (item.page === 'category' && item.category) goCategory(item.category);
  };

  const handleSettings = () => {
    setQuery('');
    goSettings();
  };

  return (
    <aside
      className={`app-sidebar ${sidebarMode} ${sidebarExpanded ? 'expanded' : 'collapsed'} fixed left-0 top-0 z-40 flex h-full flex-col`}
      style={{
        width: sidebarExpanded ? '276px' : '56px',
        background: 'var(--surface)',
        borderRight: '1px solid var(--border)',
      }}
      aria-hidden={sidebarHidden}
      inert={sidebarHidden}
    >
      <button
        className="sidebar-header sidebar-library-heading-slot"
        type="button"
        onClick={toggleSidebar}
        aria-controls="sidebar-primary-navigation"
        aria-expanded={sidebarExpanded}
        aria-label={sidebarExpanded ? '收起为图标栏' : '展开完整侧栏'}
        title={sidebarExpanded ? '收起为图标栏' : '展开完整侧栏'}
      >
        <span className="sidebar-library-heading">媒体库</span>
        <span className="sidebar-library-toggle-icon" aria-hidden="true">
          {sidebarExpanded ? <PanelLeftClose aria-hidden="true" /> : <PanelLeftOpen aria-hidden="true" />}
        </span>
      </button>

      <nav id="sidebar-primary-navigation" className="sidebar-nav flex-1 overflow-y-auto px-3">
        <div className="sidebar-nav-group sidebar-nav-group-primary">
          {primaryNavItems.map((item) => (
            <NavItem key={item.key} item={item} active={isActive(item)} expanded={sidebarExpanded} onClick={handleClick} />
          ))}
        </div>
        <div className="sidebar-nav-divider" aria-hidden="true" />
        <div className="sidebar-nav-group sidebar-nav-group-library">
          {libraryNavItems.map((item) => (
            <NavItem
              key={item.key}
              item={item}
              active={isActive(item)}
              expanded={sidebarExpanded}
              count={item.category ? categoryCounts[item.category] : undefined}
              onClick={handleClick}
            />
          ))}
        </div>
      </nav>

      <div className="sidebar-bottom px-3 pb-4 pt-4">
        <div className="sidebar-nav-divider" aria-hidden="true" />
        <button
          onClick={() => { setQuery(''); goManage(); }}
          className={`sidebar-nav-item relative w-full ${page === 'manage' ? 'active' : ''}`}
          title={sidebarExpanded ? undefined : '媒体管理'}
        >
          <NavIcon name="manage" />
          <span className="sidebar-reveal sidebar-item-label">媒体管理</span>
        </button>
        <button
          onClick={handleSettings}
          className={`sidebar-nav-item relative w-full ${page === 'settings' ? 'active' : ''}`}
          title={sidebarExpanded ? undefined : '设置'}
        >
          <NavIcon name="settings" />
          <span className="sidebar-reveal sidebar-item-label">设置</span>
        </button>

        <div className="sidebar-account-wrap mt-4 h-[76px]">
        {hasStoredCredential && user && (
          <div className="sidebar-account-card flex h-full items-center gap-3 rounded-lg border p-3" style={{ borderColor: 'var(--border)' }}>
            {user.avatar && (
              <DecodedImage
                src={buildBangumiImageUrl(user.avatar)}
                alt={user.nickname || user.username}
                className="sidebar-account-avatar h-11 w-11 rounded-full object-cover"
              />
            )}
            <div className="sidebar-reveal sidebar-account-info min-w-0 flex-1">
              <div className="truncate text-sm font-semibold" style={{ color: 'var(--text)' }} title={user.nickname || user.username}>
                {user.nickname || user.username}
              </div>
              <div className="truncate text-xs" style={{ color: 'var(--text-muted)' }} title={`Bangumi ID: ${user.id ?? user.username}`}>
                Bangumi ID: {user.id ?? user.username}
              </div>
            </div>
          </div>
        )}
        </div>
      </div>
    </aside>
  );
}

function NavItem({
  item,
  active,
  expanded,
  count,
  onClick,
}: {
  item: typeof primaryNavItems[0];
  active: boolean;
  expanded: boolean;
  count?: number;
  onClick: (item: typeof primaryNavItems[0]) => void;
}) {
  return (
    <button
      onClick={() => onClick(item)}
      className={`sidebar-nav-item relative w-full ${active ? 'active' : ''}`}
      title={expanded ? undefined : item.label}
    >
      <NavIcon name={item.icon} />
      <span className="sidebar-reveal sidebar-item-label">{item.label}</span>
      {expanded && typeof count === 'number' && (
        <span className="sidebar-category-count">{count}</span>
      )}
    </button>
  );
}

function NavIcon({ name }: { name: IconName }) {
  const icons: Record<IconName, LucideIcon> = {
    home: Home,
    favorites: Heart,
    recent: Clock3,
    seasonal: CalendarSync,
    anime: Tv,
    animeMovie: Clapperboard,
    series: MonitorPlay,
    movie: Film,
    manage: SlidersHorizontal,
    settings: Settings,
  };

  const Icon = icons[name];
  return (
    <span className="sidebar-icon grid shrink-0 place-items-center">
      <Icon aria-hidden="true" />
    </span>
  );
}
