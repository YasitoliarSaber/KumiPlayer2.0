import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { useUiStore } from '../src/stores/ui.ts';
import { useMediaWorkflowStore } from '../src/stores/mediaWorkflow.ts';
test('来源筛选同时影响作品列表与数量统计', () => {
  const library = readFileSync(new URL('../src/stores/library.ts', import.meta.url), 'utf8');

  assert.match(library, /librarySummary:\s*\(\)\s*=>\s*\{\s*const works = get\(\)\.filteredWorks\(\)/s);
});

test('媒体库刷新局部失败时保留可用内容且不切换成整页错误', () => {
  const library = readFileSync(new URL('../src/stores/library.ts', import.meta.url), 'utf8');
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');

  assert.match(library, /const libraryRequest = libraryApi\.getLibrary/);
  assert.match(library, /const historyRequest = playbackApi\.getHistory/);
  assert.match(library, /historyRequest[\s\S]*?\.catch\(\(\) =>/);
  assert.match(library, /await libraryRequest/);
  assert.match(home, /if \(error && works\.length === 0\) return <CenteredMessage>\{error\}<\/CenteredMessage>/);
});

test('媒体库维护迁入媒体管理并保留按来源同步与安全删除', () => {
  const media = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');
  const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');

  assert.match(media, /maintenance-nav-command/);
  assert.match(media, /onClick=\{\(\) => setStep\('maintenance'\)\}/);
  assert.match(media, /step === 'maintenance' && <LibraryMaintenancePanel/);
  assert.doesNotMatch(media, /const steps:[\s\S]*?key:\s*'maintenance'/);
  assert.doesNotMatch(settings, /key:\s*'maintenance'/);
  assert.match(maintenance, /rescanLibrary\(selectedSource/);
  assert.match(maintenance, /works\.filter\(\(work\) => workIncludesSource\(work, selectedSource\)\)/);
  assert.match(maintenance, /refreshedWorks\.filter\(\(work\) => workIncludesSource\(work, selectedSource\)\)/);
  assert.match(maintenance, /deleteLibraryPreview\(selectedSource/);
  assert.match(maintenance, /preview\.source\s*!==\s*selectedSource/);
  assert.match(maintenance, /deleteScopeLabel/);
  assert.match(app, /source:\s*activeSource/);
});

test('新番扫描提交当前可见范围并允许停止任务', () => {
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');
  const tracking = readFileSync(new URL('../src/api/tracking.ts', import.meta.url), 'utf8');

  assert.match(category, /trackingApi\.scanAll\(\{[\s\S]*source,[\s\S]*workIds:\s*scannableBindings\.map/);
  assert.match(category, /tasksApi\.cancel\(activeScanTaskId\)/);
  assert.match(category, />停止扫描</);
  assert.match(tracking, /scanAll:\s*\(input:/);
  assert.match(tracking, /work_ids:\s*input\.workIds/);
});

test('媒体管理细节遵循 Fluent 分层并保持窄屏清爽', () => {
  const media = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(media, /const stepIndex = flowSteps\.findIndex/);
  assert.match(media, /completedThrough=\{step === 'background' \? 1 : Math\.max\(0, stepIndex\)/);
  assert.match(media, /MediaBackgroundImportStatus/);
  assert.match(maintenance, /ListVideo/);
  assert.doesNotMatch(maintenance, /maintenance-stat-icon">集</);
  assert.match(styles, /\.media-workflow-progress ol\s*\{[^}]*display:\s*flex;/s);
  assert.match(styles, /\.seasonal-command:focus-visible,[\s\S]*\.maintenance-source-field select:focus-visible/);
});

test('媒体管理把维护入口收进统一命令层', () => {
  const media = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(media, /className=\{`maintenance-nav-command \$\{step === 'maintenance' \? 'active' : ''\}`\}/);
  // header 只承担 sticky 定位，必须透明；command-bar 是唯一 background/blur surface
  assert.match(styles, /\.media-flow-header\s*\{[^}]*background:\s*transparent[^}]*backdrop-filter:\s*none/s);
  assert.match(styles, /\.media-flow-command-bar\s*\{[^}]*grid-template-columns:\s*auto minmax\(0, 1fr\) auto[^}]*backdrop-filter:\s*blur\(14px\)/s);
  assert.doesNotMatch(styles, /\.media-flow-header\s*\{[^}]*backdrop-filter:\s*blur\(16px\)/);
});

test('媒体管理流程标签在手机宽度下隐藏文字只留图标，避免文字被挤出按钮', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(
    styles,
    /@container media-flow \(max-width:\s*480px\)\s*\{[\s\S]*?\.media-workflow-step > span:last-child\s*\{\s*display:\s*none;\s*\}/s,
  );
});

test('进入作品详情时清除搜索状态', () => {
  const store = useUiStore.getState();
  store.setQuery('石纪元');

  store.goDetail('work-dr-stone');

  const state = useUiStore.getState();
  assert.equal(state.page, 'detail');
  assert.equal(state.selectedWorkId, 'work-dr-stone');
  assert.equal(state.query, '');
});

test('从分类页进入详情再返回时保留原滚动位置，切换分类不继承旧位置', () => {
  const main = { scrollTop: 640 };
  const previousDocument = globalThis.document;
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: { querySelector: () => main },
  });

  try {
    useUiStore.setState({
      page: 'category',
      activeCategory: 'anime_series',
      selectedWorkId: null,
      source: 'all',
      navigationHistory: [],
      forwardHistory: [],
      canGoBack: false,
      canGoForward: false,
    });

    useUiStore.getState().goDetail('work-scroll');
    useUiStore.getState().goCategory('anime_series');

    assert.equal(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all'), 640);
    assert.equal(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all'), null);

    useUiStore.setState({ page: 'category', activeCategory: 'anime_series', selectedWorkId: null });
    useUiStore.getState().goDetail('work-scroll-2');
    useUiStore.getState().goCategory('anime_movie');
    assert.equal(useUiStore.getState().consumeCategoryScrollRestore('anime_movie', 'all'), null);

    // 标题栏返回（goBack 恢复历史位置）同样保留原滚动位置
    useUiStore.setState({
      page: 'category',
      activeCategory: 'anime_series',
      selectedWorkId: null,
      source: 'all',
      navigationHistory: [],
      forwardHistory: [],
      canGoBack: false,
      canGoForward: false,
    });
    useUiStore.getState().goDetail('work-scroll-3');
    useUiStore.getState().goBack();
    assert.equal(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all'), 640);

    // 从详情离开到首页后再回原分类，不应继承过期滚动位置
    useUiStore.setState({
      page: 'category',
      activeCategory: 'anime_series',
      selectedWorkId: null,
      source: 'all',
      navigationHistory: [],
      forwardHistory: [],
      canGoBack: false,
      canGoForward: false,
    });
    useUiStore.getState().goDetail('work-scroll-4');
    useUiStore.getState().goHome();
    useUiStore.getState().goCategory('anime_series');
    assert.equal(useUiStore.getState().consumeCategoryScrollRestore('anime_series', 'all'), null);
  } finally {
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: previousDocument,
    });
  }
});

test('作品详情在完整数据就绪后再切换并避免重复入场动画', () => {
  const library = readFileSync(new URL('../src/stores/library.ts', import.meta.url), 'utf8');
  const poster = readFileSync(new URL('../src/components/library/PosterCard.tsx', import.meta.url), 'utf8');
  const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(library, /await get\(\)\.getWorkDetail\(workId\)[\s\S]*commitDetailNavigation\(workId\)/);
  assert.match(library, /await waitForDetailArtwork\(work, DETAIL_ARTWORK_WAIT_MS\)[\s\S]*commitDetailNavigation\(workId\)/);
  assert.match(library, /image\.decode\(\)/);
  assert.match(library, /startViewTransition/);
  assert.match(poster, /openWorkDetail\(work\.work_id\)/);
  assert.match(detail, /openWorkDetail\(workId\)/);
  assert.doesNotMatch(app, /pageTransitionKey/);
  assert.doesNotMatch(styles, /animation:\s*app-page-(?:refresh|drill)-in/);
});

test('详情辅助状态在请求完成前不冒充未匹配结果', () => {
  const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

  assert.match(detail, /const \[auxiliaryReady, setAuxiliaryReady\] = useState\(false\)/);
  assert.match(detail, /auxiliaryReady\s*\?[^:]+:\s*'Bangumi 状态载入中'/s);
  assert.match(detail, /setAuxiliaryReady\(true\)/);
  assert.match(detail, /aria-busy=\{!auxiliaryReady\}/);
  assert.doesNotMatch(detail, /\{bangumiMatch\s*&&\s*\(\s*<span className=\{`detail-sync-status/);
});

test('详情菜单不再提供背景位置调整', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /调整背景位置/);
  assert.doesNotMatch(source, /kumiplayer-detail-background/);
});

test('详情页使用原卡片即时缩放且不恢复延迟悬停预览层', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /HOVER_PREVIEW_DELAY_MS/);
  assert.doesNotMatch(source, /detail-hover-preview/);
  assert.doesNotMatch(source, /detail-preview-stage/);
  assert.match(styles, /\.episode-button:is\(:hover, :focus-visible\) \.episode-thumb\s*\{[^}]*transform:\s*scale\(1\.18\)/s);
  assert.match(styles, /:root\[data-motion='reduced'\][\s\S]*?\.episode-button:is\(:hover, :focus-visible\) \.episode-thumb[\s\S]*?transform:\s*none\s*!important/s);
});

test('详情页辅助信息区使用递进纵向间距且不移动剧集列表', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(styles, /\.detail-page\.detail-classic-page:not\(\.detail-movie-page\) \.detail-cast-section\s*\{[^}]*margin-top:\s*clamp\(56px, 5vh, 76px\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page:not\(\.detail-movie-page\) \.detail-related-section\s*\{[^}]*margin-top:\s*clamp\(56px, 5vh, 76px\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page:not\(\.detail-movie-page\) \.detail-similar-section\s*\{[^}]*margin-top:\s*clamp\(68px, 6vh, 92px\)/s);
});

test('收藏页与分类页复用同一套卡片布局控制', () => {
  const controls = readFileSync(new URL('../src/components/library/LibraryViewControls.tsx', import.meta.url), 'utf8');
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');
  const favorites = readFileSync(new URL('../src/pages/FavoritesPage.tsx', import.meta.url), 'utf8');

  assert.match(controls, /export default function LibraryViewControls/);
  assert.match(controls, /seriesCardImageMode/);
  assert.match(controls, /setPosterSize/);
  assert.match(category, /<LibraryViewControls maxColumns=\{columnCapacity\}\s*\/>/);
  assert.match(favorites, /<LibraryViewControls maxColumns=\{columnCapacity\}\s*\/>/);
  assert.match(favorites, /<VirtualizedPosterGrid[\s\S]*?works=\{favoriteWorks\}[\s\S]*?columns=\{columnsPerRow\}[\s\S]*?onColumnCapacityChange=\{setColumnCapacity\}/);
});

test('作品列数控制最低从四列开始并在拖动结束后提交配置', () => {
  const controls = readFileSync(new URL('../src/components/library/LibraryViewControls.tsx', import.meta.url), 'utf8');

  assert.match(controls, /min=\{effectiveMinColumns\}/);
  assert.match(controls, /const MIN_COLUMNS_PER_ROW = 4/);
  assert.match(controls, /max=\{effectiveMaxColumns\}/);
  assert.match(controls, /aria-valuetext=\{`每行 \$\{visibleColumns\} 个作品`\}/);
  assert.doesNotMatch(controls, /<span>每行 \{draftColumns\}<\/span>/);
  assert.match(controls, /onInput=\{handleColumnsInput\}/);
  assert.match(controls, /onChange=\{handleColumnsInput\}/);
  assert.match(controls, /onPointerUp=\{commitDraftColumns\}/);
  assert.match(controls, /onKeyUp=\{commitDraftColumns\}/);
  assert.doesNotMatch(controls, /onChange=\{\(event\) => applyPosterSize\(Number\(event\.target\.value\)\)\}/);
});

test('分类刷新入口位于标题栏来源筛选左侧且不再占用分类工具条', () => {
  const titlebar = readFileSync(new URL('../src/components/shell/DesktopTitleBar.tsx', import.meta.url), 'utf8');
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');
  const refreshIndex = titlebar.indexOf('titlebar-library-refresh');
  const filterIndex = titlebar.indexOf('titlebar-source-filter');

  assert.ok(refreshIndex >= 0);
  assert.ok(filterIndex > refreshIndex);
  assert.match(titlebar, /page === 'category'/);
  assert.match(titlebar, /loadLibrary\(\{ force: true \}\)/);
  assert.doesNotMatch(category, /IconButton label="刷新媒体库"/);
  assert.doesNotMatch(category, /RefreshCcw/);
});

test('侧边栏使用隐藏、图标栏和完整展开三态且显隐入口位于标题栏', () => {
  const sidebar = readFileSync(new URL('../src/components/shell/Sidebar.tsx', import.meta.url), 'utf8');
  const titlebar = readFileSync(new URL('../src/components/shell/DesktopTitleBar.tsx', import.meta.url), 'utf8');
  const shell = readFileSync(new URL('../src/components/shell/AppShell.tsx', import.meta.url), 'utf8');
  const storeSource = readFileSync(new URL('../src/stores/ui.ts', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(storeSource, /export type SidebarMode = 'hidden' \| 'compact' \| 'expanded'/);
  assert.match(storeSource, /SIDEBAR_WIDTHS[\s\S]*hidden:\s*0[\s\S]*compact:\s*56[\s\S]*expanded:\s*276/);
  assert.match(storeSource, /lastVisibleSidebarMode/);
  assert.match(titlebar, /PanelLeft/);
  assert.match(titlebar, /className="titlebar-navigation-button titlebar-sidebar-visibility-button"/);
  assert.match(titlebar, /toggleSidebarVisibility/);
  assert.match(titlebar, /aria-expanded=\{!sidebarHidden\}/);
  assert.doesNotMatch(sidebar, /sidebar-rail-toggle|sidebar-edge-hover-zone|toggleSidebarVisibility/);
  assert.match(shell, /sidebar-\$\{sidebarMode\}/);
  assert.match(shell, /'--sidebar-width': `\$\{SIDEBAR_WIDTHS\[sidebarMode\]\}px`/);
  assert.match(styles, /\.app-shell\.sidebar-hidden\s*\{[^}]*--sidebar-width:\s*0px\s*!important/s);
  assert.match(styles, /\.app-shell\.sidebar-compact\s*\{[^}]*--sidebar-width:\s*56px\s*!important/s);
  assert.match(styles, /\.app-shell\.sidebar-expanded\s*\{[^}]*--sidebar-width:\s*276px\s*!important/s);
  assert.match(styles, /\.app-shell\.sidebar-hidden \.app-sidebar\s*\{[^}]*transform:\s*translateX\(-100%\)/s);
  assert.match(styles, /\.app-shell \.app-main\s*\{[^}]*transition:\s*left 180ms/s);
  assert.match(styles, /\.titlebar-sidebar-visibility-button\[aria-expanded='true'\]/);
  assert.doesNotMatch(styles, /sidebar-rail-toggle|sidebar-edge-hover-zone/);
});

test('标题栏显隐按钮恢复上一次侧边栏形态，侧栏内部按钮只切换宽窄', () => {
  useUiStore.setState({ sidebarMode: 'expanded', lastVisibleSidebarMode: 'expanded' });

  useUiStore.getState().toggleSidebarVisibility();
  assert.equal(useUiStore.getState().sidebarMode, 'hidden');
  assert.equal(useUiStore.getState().lastVisibleSidebarMode, 'expanded');

  useUiStore.getState().toggleSidebarVisibility();
  assert.equal(useUiStore.getState().sidebarMode, 'expanded');

  useUiStore.getState().collapseSidebar();
  assert.equal(useUiStore.getState().sidebarMode, 'compact');
  assert.equal(useUiStore.getState().lastVisibleSidebarMode, 'compact');

  useUiStore.getState().toggleSidebarVisibility();
  assert.equal(useUiStore.getState().sidebarMode, 'hidden');

  useUiStore.getState().toggleSidebarVisibility();
  assert.equal(useUiStore.getState().sidebarMode, 'compact');

  useUiStore.getState().toggleSidebar();
  assert.equal(useUiStore.getState().sidebarMode, 'expanded');
  assert.equal(useUiStore.getState().lastVisibleSidebarMode, 'expanded');

  useUiStore.getState().toggleSidebar();
  assert.equal(useUiStore.getState().sidebarMode, 'compact');
  assert.equal(useUiStore.getState().lastVisibleSidebarMode, 'compact');
});

test('分类排序菜单位于海报墙上层并与触发按钮左对齐', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(styles, /\.category-page \.category-head\s*\{[^}]*position:\s*relative[^}]*z-index:\s*60/s);
  assert.match(styles, /\.category-page \.sort-menu\s*\{[^}]*left:\s*0[^}]*right:\s*auto[^}]*z-index:\s*70/s);
  assert.match(styles, /\.category-page \.sort-menu\s*\{[^}]*background:\s*color-mix\(in srgb, var\(--surface-raised\) 96%, var\(--app-bg\)\)/s);
  assert.match(styles, /\.sort-menu\s*\{[^}]*width:\s*100%[^}]*box-sizing:\s*border-box/s);
  assert.match(styles, /\.sort-menu button\s*\{[^}]*display:\s*flex[^}]*justify-content:\s*space-between[^}]*gap:\s*20px/s);
});

test('分类排序菜单连续切换维度并使用轻型消隐关闭', () => {
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');

  assert.match(category, /import\s*\{\s*useDismissiblePopover\s*\}\s*from '\.\.\/hooks\/useDismissiblePopover'/);
  assert.match(category, /useDismissiblePopover\(open,[\s\S]*?onOpenChange\(false\)[\s\S]*?triggerRef\.current\?\.focus\(\)/);
  assert.match(category, /const selectSortDimension[\s\S]*?onChange\(toggleSort\(value, dimension\)\)/);
  assert.match(category, /role="menuitemradio"[\s\S]*?aria-checked=\{active\}/);
  assert.doesNotMatch(category, /onChange=\{\(value\) => \{[\s\S]*?setSortOpen\(false\)/);
});

test('详情季选择使用紧凑的 Fluent 单选下拉菜单', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const seasonPicker = readFileSync(new URL('../src/components/media/DetailSeasonPicker.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(source, /import DetailSeasonPicker from '\.\.\/components\/media\/DetailSeasonPicker'/);
  assert.match(source, /<DetailSeasonPicker[\s\S]*?selectedKey=\{selectedSeasonKey\}[\s\S]*?onSelect=\{selectDetailSeason\}/);
  assert.match(seasonPicker, /import\s*\{\s*Dropdown,\s*Option\s*\}\s*from '@fluentui\/react-components'/);
  assert.doesNotMatch(seasonPicker, /appearance="filled-darker"/);
  assert.match(seasonPicker, /positioning=\{\{\s*position:\s*'above',\s*align:\s*'start',\s*offset:\s*6,\s*matchTargetSize:\s*'width'\s*\}\}/);
  assert.match(seasonPicker, /className="detail-season-dropdown"/);
  assert.match(seasonPicker, /button=\{\{[\s\S]*?className:\s*'detail-season-trigger'/);
  assert.match(seasonPicker, /clearButton=\{null\}/);
  assert.match(seasonPicker, /listbox=\{\{\s*className:\s*'detail-season-listbox',\s*'aria-label':\s*'可选季度'\s*\}\}/);
  assert.match(seasonPicker, /onOptionSelect=\{handleOptionSelect\}/);
  assert.match(seasonPicker, /const handleOptionSelect/);
  assert.match(seasonPicker, /onSelect\(data\.optionValue\)/);
  assert.match(seasonPicker, /setOpen\(false\)/);
  assert.match(seasonPicker, /<Option key=\{season\.key\} value=\{season\.key\} text=\{season\.label\} checkIcon=\{null\}>/);
  assert.match(seasonPicker, /open=\{open\}/);
  assert.match(seasonPicker, /onOpenChange=\{handleOpenChange\}/);
  assert.match(seasonPicker, /const handleOpenChange/);
  assert.doesNotMatch(seasonPicker, /Escape|addEventListener|keydown/);
  assert.doesNotMatch(seasonPicker, /position:\s*'below'/);
  assert.doesNotMatch(source, /detail-season-tabs|detail-season-tab/);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-season-dropdown\s*\{[^}]*width:\s*112px/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-season-dropdown\s*>\s*\.detail-season-trigger\s*\{[^}]*background:\s*var\(--detail-command-surface\)/s);
  assert.match(styles, /\.detail-season-listbox\s*\{[^}]*background:\s*var\(--detail-command-flyout\)\s*!important/s);
  assert.match(styles, /\.detail-episode-heading\s*\{[^}]*gap:\s*14px/s);
  assert.doesNotMatch(styles, /\.detail-episode-heading:has\(\.detail-season-dropdown\)/);
  assert.doesNotMatch(styles, /\.detail-season-listbox\s*\{[^}]*min-width:\s*164px\s*!important/s);
  assert.doesNotMatch(styles, /\.detail-season-listbox\s*\{[^}]*width:\s*max-content\s*!important/s);
});

test('剧集详情操作组下移至与简介平齐并保持右上角独立', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-hero-actions\s*\{[^}]*top:\s*224px[^}]*bottom:\s*auto/s);
});

test('电影详情使用单片布局且不渲染剧集列表', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
  const assets = readFileSync(new URL('../src/api/assets.ts', import.meta.url), 'utf8');

  assert.match(source, /detail-movie-page/);
  assert.match(source, /isSeries && episodes\.length > 0/);
  assert.match(styles, /\.detail-page\.detail-classic-page\.detail-movie-page \.detail-hero-actions\s*\{[^}]*position:\s*static\s*!important[^}]*margin-top:\s*clamp\(/s);
  assert.match(styles, /\.detail-page\.detail-classic-page\.detail-movie-page \.detail-play-stack\s*\{[^}]*width:\s*min\(100%,\s*430px\)/s);
  assert.match(assets, /kind === 'detailBackdrop'\s*\? 'original'/s);
  assert.match(assets, /kind === 'backdrop'\s*\? 'w1280'/s);
});

test('加载状态使用可访问且可减少动态效果的电影感动效', () => {
  const source = readFileSync(new URL('../src/components/ui/loading-state.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(source, /role="status"/);
  assert.match(source, /loader-reel/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*loader-reel/);
});

test('剧集横向缩略图提供可拖动的定位滑块', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(source, /episode-strip-slider/);
  assert.match(source, /seekEpisodeStrip/);
  assert.match(styles, /\.episode-strip-slider/);
});

test('Bangumi 同步后按剧集标识稳定定位继续观看位置', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

  assert.match(source, /data-episode-id=\{episode\.episode_id\}/);
  assert.match(source, /scrollEpisodeIntoView\(strip, target, 'auto'\)/);
  assert.match(source, /target\.offsetLeft\s*-\s*\(strip\.clientWidth\s*-\s*target\.clientWidth\)\s*\/\s*2/);
});

test('详情剧集控制条在明暗背景上使用单一轻量命令层', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  for (const theme of ['fluent', 'mica', 'cinema']) {
    const themeStart = styles.lastIndexOf(`:root[data-theme='${theme}']`);
    const themeBlock = styles.slice(themeStart, styles.indexOf('}', themeStart));
    assert.match(themeBlock, /--detail-command-surface:[^]*--detail-command-fg:[^]*--detail-command-muted:/);
  }
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-toolbar\s*\{[^}]*padding:\s*3px[^}]*background:\s*var\(--detail-command-surface\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-pager\s*\{[^}]*padding:\s*0[^}]*border:\s*0[^}]*background:\s*transparent/s);
  assert.doesNotMatch(styles, /\.detail-page\.detail-classic-page \.detail-episode-pager\s*\{[^}]*background:\s*var\(--detail-command-surface\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-tool-btn,[\s\S]*?background:\s*transparent/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-tool-btn:hover,[\s\S]*?background:\s*var\(--detail-command-hover\)/s);
  assert.match(styles, /\.detail-season-dropdown\s*>\s*\.detail-season-trigger:hover\s*\{[^}]*background:\s*var\(--detail-command-surface-hover\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-pager button\s*\{[^}]*color:\s*var\(--detail-command-fg\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.episode-strip-slider\s*\{[^}]*background:\s*transparent/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.episode-strip-slider::-webkit-slider-runnable-track\s*\{[^}]*background:\s*linear-gradient\([^}]*var\(--range-track\)/s);
  const micaStart = styles.lastIndexOf(":root[data-theme='mica'] {");
  const micaBlock = styles.slice(micaStart, styles.indexOf('}', micaStart));
  assert.match(micaBlock, /--detail-command-fg:\s*rgb\(248 250 252 \/ \.98\)/);
  assert.match(micaBlock, /--detail-command-surface:\s*rgb\(14 24 30 \/ \.44\)/);
  assert.match(micaBlock, /--detail-command-flyout:\s*rgb\(18 22 26 \/ \.90\)/);
  const micaCommandTokens = micaBlock.match(/--range-track:[\s\S]*?--detail-command-shadow:[^\n]+/)?.[0] || '';
  assert.doesNotMatch(micaCommandTokens, /55 53 47/);
});

test('Bangumi 头像使用桌面后端图片地址构造器', () => {
  const api = readFileSync(new URL('../src/api/bangumi.ts', import.meta.url), 'utf8');
  const sidebar = readFileSync(new URL('../src/components/shell/Sidebar.tsx', import.meta.url), 'utf8');
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');

  assert.match(api, /export function buildBangumiImageUrl/);
  assert.match(sidebar, /buildBangumiImageUrl\(user\.avatar\)/);
  assert.match(settings, /buildBangumiImageUrl\(user\.avatar\)/);
});

test('Bangumi 启动恢复区分已保存凭据与临时连接失败', () => {
  const api = readFileSync(new URL('../src/api/bangumi.ts', import.meta.url), 'utf8');
  const store = readFileSync(new URL('../src/stores/bangumi.ts', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');

  assert.match(api, /getSession/);
  assert.match(store, /saved_offline/);
  assert.match(store, /restoreSession/);
  assert.match(app, /sessionStatus\s*!==\s*'saved_offline'/);
  assert.match(app, /restoreSession\(1\)/);
  assert.match(settings, /登录信息已保存/);
  assert.match(settings, /重新连接/);
});

test('Bangumi 每次恢复连接只刷新当前已打开作品', () => {
  const store = readFileSync(new URL('../src/stores/bangumi.ts', import.meta.url), 'utf8');
  const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

  assert.match(detail, /useBangumiStore\(\(state\) => state\.sessionStatus\)/);
  assert.match(detail, /previousBangumiSessionStatusRef/);
  assert.match(detail, /previousStatus === 'connected'/);
  assert.match(detail, /bangumiSessionStatus !== 'connected'/);
  assert.match(detail, /loadAuxiliary\(\{ preferCache: false \}\)/);
  assert.doesNotMatch(detail, /observedBangumiConnectionRevisionRef/);
  assert.doesNotMatch(store, /loadLibrary|workIds|syncAll/);
});

test('设置页使用左侧分类导航、模块卡片和双路径 Bangumi 登录', () => {
  const source = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(source, /settings-outline-icon/);
  assert.match(source, /settings-outline-copy/);
  assert.match(source, /settings-account-hero/);
  assert.match(source, /创建 Bangumi 个人访问令牌/);
  assert.match(source, /settings-token-login/);
  assert.match(source, /BANGUMI_ACCESS_TOKEN_URL/);
  assert.match(styles, /\.settings-shell\.settings-shell-settings\s*\{[^}]*grid-template-columns:\s*220px minmax\(0,\s*1fr\)/s);
});

test('低频大页面按需加载且页面切换不再显示准备中遮罩', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
  const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');

  assert.match(app, /lazy\(\(\) => import\('\.\/pages\/WorkDetailPage'\)\)/);
  assert.match(app, /lazy\(\(\) => import\('\.\/pages\/MediaManagementPage'\)\)/);
  assert.match(app, /lazy\(\(\) => import\('\.\/pages\/SettingsPage'\)\)/);
  assert.match(app, /<Suspense fallback=\{null\}>/);
  assert.doesNotMatch(app, /正在准备页面|正在载入界面组件/);
  assert.match(app, /key=\{visiblePage\}/);
  assert.match(app, /data-page=\{visiblePage\}/);
  assert.match(styles, /@keyframes app-page-enter/);
  assert.match(styles, /data-motion='reduced'[^}]*\.app-page-transition/s);
  assert.doesNotMatch(settings, /label="后端端口"/);
  assert.doesNotMatch(settings, /DeepSeek/);
});

test('切换媒体来源时清除上一个来源的新番状态', () => {
  const store = useMediaWorkflowStore.getState();
  store.setImportScope('seasonal');

  store.setSource(store.source === 'pan115' ? 'baidu' : 'pan115');

  assert.equal(useMediaWorkflowStore.getState().importScope, '');
});

test('拖入的目录树路径只消费一次', () => {
  const store = useMediaWorkflowStore.getState();
  store.queueDroppedTreePath('H:\\目录树\\动画.txt');

  assert.equal(useMediaWorkflowStore.getState().pendingDroppedTreePath, 'H:\\目录树\\动画.txt');
  assert.equal(useMediaWorkflowStore.getState().consumeDroppedTreePath(), 'H:\\目录树\\动画.txt');
  assert.equal(useMediaWorkflowStore.getState().pendingDroppedTreePath, null);
  assert.equal(useMediaWorkflowStore.getState().consumeDroppedTreePath(), null);
});

test('全局 TXT 拖放复用媒体管理导入流程并排除详情与设置页', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const fileDrop = readFileSync(new URL('../src/platform/fileDrop.ts', import.meta.url), 'utf8');
  const media = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const client = readFileSync(new URL('../src/api/mediaPresets.ts', import.meta.url), 'utf8');

  assert.match(app, /listenForTreeFileDrop/);
  assert.match(app, /page === 'detail' \|\| page === 'settings'/);
  assert.match(app, /queueDroppedTreePath\(event\.paths\[0\]\)/);
  assert.match(app, /goManage\(\)/);
  assert.match(fileDrop, /paths\.length !== 1/);
  assert.match(fileDrop, /extension === 'txt'/);
  assert.match(media, /consumeDroppedTreePath/);
  assert.match(media, /mediaPresetsApi\.createFromPath/);
  assert.match(client, /\/api\/media-presets\/import-local-tree/);
  assert.doesNotMatch(media, /DroppedTreeDialog|拖放导入弹窗/);
});

test('新番目录树导入使用红色警示和二次确认', () => {
  const source = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(source, /media-import-scope-warning/);
  assert.match(source, /当前选择的是新番（追更中）/);
  assert.match(source, /aria-label="确认新番导入"/);
  assert.match(source, /确认按新番导入/);
  assert.match(styles, /\.media-import-setup\.seasonal-risk/);
  assert.match(styles, /\.media-seasonal-confirm-dialog/);
});

test('新番目录树与页面扫描共用自动更新语义且不保留无效补救入口', () => {
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(category, /扫描更新当前/);
  assert.doesNotMatch(category, /保守扫描/);
  assert.doesNotMatch(category, /重新自动处理/);
  assert.doesNotMatch(category, /打开作品/);
  assert.doesNotMatch(category, /查看处理详情/);
  assert.doesNotMatch(styles, /seasonal-attention/);
  assert.doesNotMatch(management, /按文件名前缀定位配置根下的媒体目录/);
  assert.match(management, /导入新版并安全比对/);
});

test('导入流程的人工推进动作使用统一醒目的主命令按钮', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const workbench = readFileSync(new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(management, /className="media-primary-command"[^>]*icon=\{<Plus/);
  assert.match(workbench, /className="media-primary-command"[^>]*onClick=\{onStart\}/);
  assert.match(styles, /\.media-flow-page \.media-primary-command\.fui-Button\s*\{[^}]*min-height:\s*42px[^}]*background:/s);
  assert.match(styles, /\.media-flow-page \.media-primary-command\.fui-Button:focus-visible/);
});

test('导入卡片删除只清理卡片与目录树比对归档', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const client = readFileSync(new URL('../src/api/mediaPresets.ts', import.meta.url), 'utf8');

  assert.match(client, /deletePreview:/);
  assert.match(client, /deleteConfirm:/);
  assert.match(management, /删除导入卡片/);
  assert.match(management, /删除卡片与目录树版本/);
  assert.match(management, /镜像、NFO、图片、刮削结果、媒体库、追更和观看状态均会保留/);
  assert.match(management, /请前往“媒体库维护”按来源操作/);
  assert.doesNotMatch(management, /彻底删除媒体库/);
  assert.doesNotMatch(management, /presetDeleteOptions/);
  assert.match(management, /mediaPresetsApi\.deletePreview/);
  assert.match(management, /mediaPresetsApi\.deleteConfirm/);
  assert.match(management, /if \(!result\.deleted_preset\)/);
  assert.match(management, /archive_version_count/);
  assert.match(client, /preserved_generated_media/);
  assert.match(client, /preserved_library_data/);
  assert.doesNotMatch(client, /generated_file_count/);
  assert.doesNotMatch(client, /playback_record_count/);
});

test('媒体库删除失败在弹窗内反馈且确认按钮不会溢出', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(management, /deleteDialogError/);
  assert.match(management, /className="media-delete-dialog-error" role="alert"/);
  assert.match(management, /setDeleteDialogError\(''\)/);
  assert.match(management, /deletingPresetId \? '正在删除' : '确认删除卡片'/);
  assert.match(styles, /\.media-delete-dialog footer\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:/s);
  assert.match(styles, /\.media-delete-dialog footer \.fui-Button\s*\{[^}]*min-width:\s*0;[^}]*width:\s*100%;/s);
  assert.match(styles, /@media \(max-width:\s*640px\)\s*\{[^}]*\.media-delete-dialog footer\s*\{[^}]*grid-template-columns:\s*1fr;/s);
});

test('镜像生成明确抽样上限并在主区域持续显示运行状态', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const workbench = readFileSync(new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(management, /开始前最多抽样验证 3 个代表视频/);
  assert.match(workbench, /media-task-overview/);
  assert.match(workbench, /active \? <Spinner size="tiny" \/>/);
  assert.match(workbench, /任务正在运行/);
  assert.match(styles, /\.media-task-status\s*\{[^}]*display:\s*inline-flex;/s);
  assert.match(styles, /\.media-log-running\s*\{[^}]*display:\s*flex;/s);
});

test('媒体库维护按当前来源生成删除预览且不保留隐式清理选项', () => {
  const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');

  assert.match(maintenance, /deleteLibraryPreview\(selectedSource/);
  assert.match(maintenance, /preview\.source !== selectedSource/);
  assert.doesNotMatch(maintenance, /preserve_seasonal/);
  assert.doesNotMatch(maintenance, /清空当前范围/);
});

test('媒体库维护使用紧凑命令层并隔离危险操作', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

  assert.match(management, /maintenance-nav-command/);
  assert.match(maintenance, /className="maintenance-summary-strip"/);
  assert.match(maintenance, /className="maintenance-command-surface"/);
  assert.match(maintenance, /aria-label="刷新媒体库状态"/);
  assert.match(maintenance, /className="maintenance-command primary"/);
  assert.match(maintenance, />同步索引<\/Button>/);
  assert.match(maintenance, /className="maintenance-command danger"/);
  assert.match(maintenance, /className="maintenance-danger-zone"/);
  assert.doesNotMatch(maintenance, /“需人工检查”表示索引与刮削记录不一致/);
  assert.doesNotMatch(maintenance, /这是唯一的全局删除入口，会清理全部来源/);
  assert.match(styles, /\.library-maintenance-stage\s*\{[^}]*max-width:\s*1280px[^}]*margin:\s*0 auto/s);
  assert.match(styles, /\.maintenance-summary-strip\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/s);
  assert.match(styles, /\.maintenance-command\.icon-only\.fui-Button\s*\{[^}]*width:\s*38px[^}]*padding:\s*0/s);
  assert.match(styles, /\.maintenance-command\.primary\.fui-Button\s*\{[^}]*min-height:\s*38px/s);
  assert.match(styles, /\.maintenance-danger-zone\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto/s);
});

test('单卡删除预览生成期间可以立即取消并中止前端请求', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const presetsApi = readFileSync(new URL('../src/api/mediaPresets.ts', import.meta.url), 'utf8');

  assert.match(management, /deletePreviewAbortRef/);
  assert.match(management, /new AbortController\(\)/);
  assert.match(management, /deletePreviewAbortRef\.current\?\.abort\(\)/);
  assert.match(management, /mediaPresetsApi\.deletePreview\(deletingPreset\.preset_id, controller\.signal\)/);
  assert.match(management, /disabled=\{Boolean\(deletingPresetId\)\} onClick=\{closePresetDelete\}>取消<\/Button>/);
  assert.match(presetsApi, /deletePreview: \(presetId: string, signal\?: AbortSignal\)/);
});

test('镜像全部已存在仍显示明确状态并在完成后刷新媒体库', () => {
  const management = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const workbench = readFileSync(new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url), 'utf8');

  assert.match(workbench, /复用 \$\{metrics\.skipped\} 项/);
  assert.match(management, /taskKind !== 'mirror'/);
  assert.match(management, /loadLibrary\(\{ force: true \}\)/);
});

test('详情页明确区分同系列关联作品与算法推荐', () => {
  const source = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

  assert.match(source, />关联作品<\/h2>/);
  assert.match(source, />相关推荐<\/h2>/);
  assert.doesNotMatch(source, />相似作品<\/h2>/);
});
