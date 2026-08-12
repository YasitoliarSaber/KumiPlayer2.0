import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const systemApi = readFileSync(new URL('../src/api/system.ts', import.meta.url), 'utf8');

test('作品简介默认收起且可点击展开和收起', () => {
  assert.match(detail, /plotExpanded/);
  assert.match(detail, /aria-expanded=\{plotExpanded\}/);
  assert.match(detail, /setPlotExpanded\(\(expanded\) => !expanded\)/);
  assert.match(styles, /\.detail-hero-plot\.is-expanded\s*\{[^}]*-webkit-line-clamp:\s*unset/s);
  // 折叠态锁定多行截断四属性，省略号由 -webkit-line-clamp 原生生成，不叠加任何遮罩伪元素。
  assert.match(
    styles,
    /\.detail-hero-plot:not\(\.is-expanded\)\s*\{[^}]*display:\s*-webkit-box[^}]*overflow:\s*hidden[^}]*-webkit-box-orient:\s*vertical[^}]*-webkit-line-clamp:\s*3\s*!important/s,
  );
  assert.doesNotMatch(styles, /\.detail-hero-plot:not\(\.is-expanded\)::after/);
});

test('更多菜单支持持久化修改作品标题', () => {
  assert.match(detail, /修改作品标题/);
  assert.match(detail, /libraryApi\.setWorkTitle/);
  assert.match(detail, /恢复刮削标题/);
});

test('播放按钮静止时显示播放信息，悬停时居中显示当前集和百分比', () => {
  assert.match(detail, /detail-continue-percent/);
  assert.match(detail, /continuePercent > 0 && \(/);
  assert.match(detail, /\{continuePercent\}%/);
  assert.match(detail, /<strong>开始播放<\/strong>[\s\S]*?<small>/);
  assert.match(detail, /detail-continue-hover-copy/);
  assert.match(styles, /\.detail-continue-btn:is\(:hover, :focus-visible\) \.detail-continue-copy\s*\{[^}]*opacity:\s*0/s);
  assert.match(styles, /\.detail-continue-btn:is\(:hover, :focus-visible\) \.detail-continue-hover-state,[\s\S]*?opacity:\s*1/s);
  assert.match(styles, /\.detail-continue-btn:is\(:hover, :focus-visible\) \.detail-continue-percent\s*\{[^}]*opacity:\s*1/s);
});

test('电影播放按钮只显示刮削后的中文作品标题', () => {
  assert.match(detail, /const isMovie = work\.media_type === 'movie'/);
  assert.match(detail, /const continueCompactLabel = !isMovie[\s\S]{0,240}: work\.title;/);
  assert.match(detail, /const continueHoverLabel = !isMovie[\s\S]{0,240}: work\.title;/);
  assert.match(detail, /<small>\{continueCompactLabel\}<\/small>/);
  assert.match(detail, /className="detail-continue-hover-copy">\s*\{continueHoverLabel\}/);
});

test('混合来源卡手动刮削会跨来源查找当前季度', () => {
  assert.match(detail, /const scrapeTargetSource = hasMultipleSources \? undefined : work\.source;/);
  assert.match(detail, /getTargetByWork\([\s\S]{0,180}scrapeTargetSource/);
});

test('手动刮削锚定当前卡片且提供作品删除确认', () => {
  assert.match(detail, /scrapeApi\.selectCandidate\([\s\S]{0,320}work\.work_id/);
  assert.match(detail, /删除该作品/);
  assert.match(detail, /libraryApi\.deleteWorkPreview\(work\.work_id\)/);
  assert.match(detail, /libraryApi\.deleteWorkConfirm/);
  assert.match(detail, /无法撤销/);
  assert.match(detail, /Trash2/);
});

test('Bangumi 状态标签打开同步面板且更多菜单不再重复入口', () => {
  const menuStart = detail.indexOf('{moreMenuOpen && <div className="detail-more-menu"');
  const menuEnd = detail.indexOf('</div>}', menuStart);
  const moreMenu = detail.slice(menuStart, menuEnd);

  assert.match(detail, /<button[\s\S]*?className=\{`detail-sync-status[\s\S]*?setBangumiPanelOpen/);
  assert.match(detail, /aria-expanded=\{bangumiPanelOpen\}/);
  assert.doesNotMatch(moreMenu, /Bangumi 同步/);
  assert.match(styles, /\.detail-sync-status:not\(:disabled\)[\s\S]*?cursor:\s*pointer/);
});

test('Bangumi 同步面板支持点击外部和 Esc 关闭', () => {
  assert.match(detail, /const bangumiPanelRef = useRef<HTMLDivElement>\(null\)/);
  assert.match(detail, /useDismissiblePopover\(bangumiPanelOpen,[\s\S]{0,180}bangumiPanelRef\)/);
  assert.match(detail, /className="detail-tool-overlay"[\s\S]{0,240}ref=\{bangumiPanelRef\}/);
  assert.match(detail, /aria-modal="true"/);
  assert.match(styles, /body > \.detail-tool-overlay\s*\{[^}]*position:\s*fixed[^}]*inset:/s);
});

test('详情操作反馈不再占据播放按钮并自动收起', () => {
  assert.doesNotMatch(detail, /detail-action-notice/);
  assert.match(detail, /detail-action-toast/);
  assert.match(detail, /setTimeout\(\(\) => setNotice\(''\),\s*\d+\)/);
  assert.match(styles, /\.detail-action-toast\s*\{[^}]*position:\s*fixed/s);
  assert.doesNotMatch(styles, /\.detail-action-notice/);
});

test('更多菜单在视频文件夹下方提供镜像文件夹入口', () => {
  const videoFolderEntry = detail.indexOf('打开视频文件夹');
  const mirrorFolderEntry = detail.indexOf('打开镜像文件夹');
  assert.ok(videoFolderEntry >= 0);
  assert.ok(mirrorFolderEntry > videoFolderEntry);
  assert.match(detail, /handleOpenMirrorFolder/);
  assert.match(systemApi, /openMirrorFolder/);
  assert.match(systemApi, /folder_type:\s*'mirror'/);
});

test('混合来源详情显示全部标签并按来源剧集定位文件夹', () => {
  assert.match(detail, /workSources\.map\(\(source\)/);
  assert.match(detail, /workSourceLabel\(source\)/);
  assert.match(detail, /sourceFolderEpisodes\.map\(\(\{ source, episodeId \}\)/);
  assert.match(detail, /work\.source_episode_ids\?\.\[source\]/);
  assert.match(detail, /hasDetailTags[^;]*workSources\.length > 0/);
  assert.match(detail, /handleOpenFolder\(episodeId, source\)/);
  assert.match(detail, /handleOpenMirrorFolder\(episodeId, source\)/);
  assert.match(systemApi, /openMirrorFolder\(workId: string, episodeId\?: string/);
  assert.match(systemApi, /episode_id: episodeId \|\| ''/);
});

test('详情页进度刷新串行调度且不会叠加轮询请求', () => {
  assert.doesNotMatch(detail, /setInterval\([\s\S]{0,180}refreshPlaybackSnapshot/);
  assert.match(detail, /window\.setTimeout\(async \(\) => \{[\s\S]{0,220}await refreshPlaybackSnapshot\(\)/);
  assert.match(detail, /if \(!disposed\) scheduleNextRefresh\(\)/);
  assert.match(detail, /window\.clearTimeout\(timer\)/);
});

test('已完成历史集保留为主播放按钮上下文，空目标不回落第一集', () => {
  const resolveStart = detail.indexOf('function resolveContinueEpisode(');
  const resolveEnd = detail.indexOf('\n}\n', resolveStart);
  const resolveBody = detail.slice(resolveStart, resolveEnd);

  // 历史集无论是否已完成都作为当前显示上下文，完成瞬间不选择全季第一集未观看。
  assert.match(resolveBody, /if \(historyEpisode\) return historyEpisode;/);
  assert.doesNotMatch(resolveBody, /watchedEpisodeIds\.has\(episode\.episode_id\)\) \|\| historyEpisode/);

  // 空目标或刷新空档时主按钮不静默回退到第 1 集，保留已有上下文。
  const playStart = detail.indexOf('const handlePlay = async (episodeId?: string) => {');
  const playEnd = detail.indexOf('};', playStart);
  const playBody = detail.slice(playStart, playEnd);

  assert.match(playBody, /episodeId \|\| continueTarget\?\.episode_id \|\| ''/);
  assert.doesNotMatch(playBody, /episodes\[0\]\?\.episode_id/);
});
