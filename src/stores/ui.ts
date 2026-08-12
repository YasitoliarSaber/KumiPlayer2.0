import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type AppPage = 'home' | 'favorites' | 'recent' | 'category' | 'detail' | 'manage' | 'settings' | 'player-tuning';
export type CategoryKey = 'anime_series' | 'anime_movie' | 'live_series' | 'live_movie';
export type LibraryView = CategoryKey | 'seasonal';
export type StyleMode = 'gallery';
export type AppearanceMode = 'fluent' | 'cinema' | 'mica';
export type MotionMode = 'reduced' | 'standard' | 'expressive';
export type SortId = 'recent' | 'title' | 'titleDesc' | 'rating' | 'year' | 'ratingDesc' | 'ratingAsc' | 'episodesDesc' | 'episodesAsc' | 'yearAsc' | 'yearDesc';
export type SourceId = 'all' | 'pan115' | 'baidu' | 'local' | 'openlist';
export type SeriesCardImageMode = 'poster' | 'fanart';
export type SidebarMode = 'hidden' | 'compact' | 'expanded';
type VisibleSidebarMode = Exclude<SidebarMode, 'hidden'>;

export interface CategoryScrollRestore {
  category: LibraryView;
  source: SourceId;
  scrollTop: number;
}

export const SIDEBAR_WIDTHS: Record<SidebarMode, number> = {
  hidden: 0,
  compact: 56,
  expanded: 276,
};

export interface WorkSeasonSelection {
  seasonNumber: number | null;
  seasonKey: string;
}

interface NavigationLocation {
  page: AppPage;
  activeCategory: LibraryView | null;
  selectedWorkId: string | null;
  query: string;
}

const MAX_NAVIGATION_HISTORY = 50;

interface UiState {
  page: AppPage;
  activeCategory: LibraryView | null;
  selectedWorkId: string | null;
  navigationHistory: NavigationLocation[];
  forwardHistory: NavigationLocation[];
  canGoBack: boolean;
  canGoForward: boolean;
  sidebarMode: SidebarMode;
  lastVisibleSidebarMode: VisibleSidebarMode;
  query: string;
  sort: SortId;
  source: SourceId;
  posterSize: number;
  styleMode: StyleMode;
  motionMode: MotionMode;
  appearanceMode: AppearanceMode;
  seriesCardImageMode: SeriesCardImageMode;
  selectedSeasonNumber: number | null;
  selectedSeasonByWork: Record<string, WorkSeasonSelection>;
  categoryScrollRestore: CategoryScrollRestore | null;

  goHome: () => void;
  goFavorites: () => void;
  goRecent: () => void;
  goCategory: (category: LibraryView) => void;
  goDetail: (workId: string) => void;
  goManage: () => void;
  goSettings: () => void;
  goPlayerTuning: () => void;
  goBack: () => void;
  goForward: () => void;
  selectSeason: (seasonNumber: number | null) => void;
  rememberWorkSeason: (workId: string, selection: WorkSeasonSelection) => void;
  setQuery: (query: string) => void;
  setSort: (sort: SortId) => void;
  setSource: (source: SourceId) => void;
  setPosterSize: (size: number) => void;
  setSeriesCardImageMode: (mode: SeriesCardImageMode) => void;
  setAppearanceMode: (mode: AppearanceMode) => void;
  setActiveCategory: (category: LibraryView | null) => void;
  toggleSidebar: () => void;
  toggleSidebarVisibility: () => void;
  expandSidebar: () => void;
  collapseSidebar: () => void;
  focusSearch: () => void;
  consumeCategoryScrollRestore: (category: LibraryView, source: SourceId) => number | null;
}

function currentLocation(state: UiState): NavigationLocation {
  return {
    page: state.page,
    activeCategory: state.activeCategory,
    selectedWorkId: state.selectedWorkId,
    query: state.query,
  };
}

function navigate(state: UiState, target: NavigationLocation): Partial<UiState> {
  const current = currentLocation(state);
  const unchanged = current.page === target.page
    && current.activeCategory === target.activeCategory
    && current.selectedWorkId === target.selectedWorkId
    && current.query === target.query;

  if (unchanged) return target;

  const navigationHistory = [...state.navigationHistory, current].slice(-MAX_NAVIGATION_HISTORY);
  return {
    ...target,
    navigationHistory,
    forwardHistory: [],
    canGoBack: navigationHistory.length > 0,
    canGoForward: false,
  };
}

export const useUiStore = create<UiState>()(
  persist(
    (set, get) => ({
      page: 'home',
      activeCategory: null,
      selectedWorkId: null,
      navigationHistory: [],
      forwardHistory: [],
      canGoBack: false,
      canGoForward: false,
      sidebarMode: 'expanded',
      lastVisibleSidebarMode: 'expanded',
      query: '',
      sort: 'recent',
      source: 'all',
      posterSize: 180,
      styleMode: 'gallery',
      motionMode: 'standard',
      appearanceMode: 'fluent',
      seriesCardImageMode: 'poster',
      selectedSeasonNumber: null,
      selectedSeasonByWork: {},
      categoryScrollRestore: null,

      goHome: () => set((state) => ({ ...navigate(state, { page: 'home', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goFavorites: () => set((state) => ({ ...navigate(state, { page: 'favorites', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goRecent: () => set((state) => ({ ...navigate(state, { page: 'recent', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goCategory: (category) => set((state) => {
        // 只有从详情页返回当前正在浏览的分类时才保留待恢复的滚动位置；
        // 其他分类导航一律清空，避免过期位置污染下一次进入。
        const keepRestore = state.page === 'detail' && state.activeCategory === category;
        return {
          ...navigate(state, { page: 'category', activeCategory: category, selectedWorkId: null, query: '' }),
          categoryScrollRestore: keepRestore ? state.categoryScrollRestore : null,
        };
      }),
      goDetail: (workId) => set((state) => {
        let restore: CategoryScrollRestore | null = null;
        if (state.page === 'category' && state.activeCategory) {
          const main = document.querySelector<HTMLElement>('.app-main');
          if (main) {
            restore = {
              category: state.activeCategory,
              source: state.source,
              scrollTop: main.scrollTop,
            };
          }
        }
        return {
          ...navigate(state, { page: 'detail', activeCategory: state.activeCategory, selectedWorkId: workId, query: '' }),
          ...(restore ? { categoryScrollRestore: restore } : {}),
        };
      }),
      goManage: () => set((state) => ({ ...navigate(state, { page: 'manage', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goSettings: () => set((state) => ({ ...navigate(state, { page: 'settings', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goPlayerTuning: () => set((state) => ({ ...navigate(state, { page: 'player-tuning', activeCategory: null, selectedWorkId: null, query: '' }), categoryScrollRestore: null })),
      goBack: () => set((state) => {
        const target = state.navigationHistory.at(-1);
        if (!target) return state;
        const navigationHistory = state.navigationHistory.slice(0, -1);
        const forwardHistory = [...state.forwardHistory, currentLocation(state)].slice(-MAX_NAVIGATION_HISTORY);
        return {
          ...target,
          navigationHistory,
          forwardHistory,
          canGoBack: navigationHistory.length > 0,
          canGoForward: true,
        };
      }),
      goForward: () => set((state) => {
        const target = state.forwardHistory.at(-1);
        if (!target) return state;
        const navigationHistory = [...state.navigationHistory, currentLocation(state)].slice(-MAX_NAVIGATION_HISTORY);
        const forwardHistory = state.forwardHistory.slice(0, -1);
        return {
          ...target,
          navigationHistory,
          forwardHistory,
          canGoBack: true,
          canGoForward: forwardHistory.length > 0,
        };
      }),
      selectSeason: (seasonNumber) => set({ selectedSeasonNumber: seasonNumber }),
      rememberWorkSeason: (workId, selection) => set((state) => ({
        selectedSeasonByWork: {
          ...state.selectedSeasonByWork,
          [workId]: selection,
        },
      })),
      setQuery: (query) => set({ query }),
      setSort: (sort) => set({ sort }),
      setSource: (source) => set({ source }),
      setPosterSize: (size) => set({ posterSize: size }),
      setSeriesCardImageMode: (mode) => set({ seriesCardImageMode: mode }),
      setAppearanceMode: (mode) => set({ appearanceMode: mode }),
      setActiveCategory: (category) => set({ activeCategory: category }),
      toggleSidebar: () => set((state) => {
        const visibleMode = state.sidebarMode === 'hidden'
          ? state.lastVisibleSidebarMode
          : state.sidebarMode;
        const nextMode = visibleMode === 'expanded' ? 'compact' : 'expanded';
        return { sidebarMode: nextMode, lastVisibleSidebarMode: nextMode };
      }),
      toggleSidebarVisibility: () => set((state) => (
        state.sidebarMode === 'hidden'
          ? { sidebarMode: state.lastVisibleSidebarMode }
          : { sidebarMode: 'hidden', lastVisibleSidebarMode: state.sidebarMode }
      )),
      expandSidebar: () => set({ sidebarMode: 'expanded', lastVisibleSidebarMode: 'expanded' }),
      collapseSidebar: () => set({ sidebarMode: 'compact', lastVisibleSidebarMode: 'compact' }),
      focusSearch: () => set({ sidebarMode: 'expanded', lastVisibleSidebarMode: 'expanded' }),
      consumeCategoryScrollRestore: (category, source) => {
        const restore = get().categoryScrollRestore;
        set({ categoryScrollRestore: null });
        if (!restore) return null;
        return restore.category === category && restore.source === source ? restore.scrollTop : null;
      },
    }),
    {
      name: 'kumiplayer-ui',
      version: 4,
      migrate: (persistedState) => {
        const state = persistedState as any;
        const migrated = { ...state };
        if (!['hidden', 'compact', 'expanded'].includes(migrated.sidebarMode)) {
          migrated.sidebarMode = migrated.sidebarExpanded === false ? 'compact' : 'expanded';
        }
        if (!['compact', 'expanded'].includes(migrated.lastVisibleSidebarMode)) {
          migrated.lastVisibleSidebarMode = migrated.sidebarMode === 'compact' ? 'compact' : 'expanded';
        }
        delete migrated.sidebarExpanded;
        if (migrated.appearanceMode === ('mono' as AppearanceMode)) {
          migrated.appearanceMode = 'mica' as AppearanceMode;
        }
        return migrated;
      },
      partialize: (state) => ({
        sidebarMode: state.sidebarMode,
        lastVisibleSidebarMode: state.lastVisibleSidebarMode,
        posterSize: state.posterSize,
        styleMode: state.styleMode,
        motionMode: state.motionMode,
        appearanceMode: state.appearanceMode,
        sort: state.sort,
        seriesCardImageMode: state.seriesCardImageMode,
        selectedSeasonByWork: state.selectedSeasonByWork,
      }),
    }
  )
);
