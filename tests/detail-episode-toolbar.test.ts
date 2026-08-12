import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';


const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
const seasonPicker = readFileSync(new URL('../src/components/media/DetailSeasonPicker.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const settings = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');


test('详情页删除跨季度全部筛选及其前端状态', () => {
  assert.doesNotMatch(detail, /showAllEpisodes/);
  assert.doesNotMatch(detail, /setShowAllEpisodes/);
  assert.doesNotMatch(detail, /显示全部季度/);
  assert.doesNotMatch(detail, /episode-season-label/);
  assert.doesNotMatch(detail, />全部</);
});


test('剧集列表始终只展示当前选择的季度', () => {
  assert.match(detail, /episode\.season_number === selectedSeason\.season_number/);
  assert.match(detail, /normalizedGroupType\(episode\.group_type\) === normalizedGroupType\(selectedSeason\.group_type\)/);
  assert.doesNotMatch(detail, /if \(showAllEpisodes\) return true/);
});


test('季度选择以紧凑单选下拉紧邻剧集标题', () => {
  assert.match(detail, /className="detail-episode-toolbar" role="toolbar" aria-label="剧集浏览工具"/);
  const headingStart = detail.indexOf('className="detail-episode-heading"');
  const toolbarStart = detail.indexOf('className="detail-episode-toolbar"');
  const seasonDropdownStart = detail.indexOf('<DetailSeasonPicker', headingStart);
  assert.ok(headingStart >= 0 && seasonDropdownStart > headingStart && seasonDropdownStart < toolbarStart);
  assert.match(detail, /className="detail-episode-head[^"]* justify-between/);
  assert.doesNotMatch(detail, /episodeSortDirection|setEpisodeSortDirection|正序|倒序/);
  assert.doesNotMatch(detail, /detail-season-tabs|detail-season-tab/);
  assert.doesNotMatch(detail, /新的右侧季度区域|right-season/);
  assert.match(seasonPicker, /className="detail-season-dropdown"/);
  assert.match(seasonPicker, /button=\{\{[\s\S]*?className:\s*'detail-season-trigger'/);
  assert.doesNotMatch(seasonPicker, /appearance="filled-darker"/);
  assert.match(seasonPicker, /expandIcon=\{\{ className: 'detail-season-chevron'/);
  assert.match(seasonPicker, /clearButton=\{null\}/);
  assert.match(seasonPicker, /selectedOptions=\{selectedKey \? \[selectedKey\] : \[\]\}/);
  assert.match(seasonPicker, /onOptionSelect=\{handleOptionSelect\}/);
  assert.match(seasonPicker, /const handleOptionSelect/);
  assert.match(seasonPicker, /onSelect\(data\.optionValue\)/);
  assert.match(seasonPicker, /setOpen\(false\)/);
  assert.match(seasonPicker, /open=\{open\}/);
  assert.doesNotMatch(seasonPicker, /Escape|addEventListener|keydown/);
  assert.match(seasonPicker, /seasons\.map\(\(season\) =>/);
  assert.match(seasonPicker, /<Option key=\{season\.key\} value=\{season\.key\} text=\{season\.label\} checkIcon=\{null\}>/);
  assert.doesNotMatch(detail, /detail-season-tabs|detail-season-tab/);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-toolbar\s*\{[^}]*flex-wrap:\s*nowrap[^}]*overflow-x:\s*auto/s);
  assert.match(seasonPicker, /positioning=\{\{\s*position:\s*'above',\s*align:\s*'start',\s*offset:\s*6,\s*matchTargetSize:\s*'width'\s*\}\}/);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-season-dropdown\s*\{[^}]*width:\s*112px[^}]*height:\s*32px/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-season-dropdown\s*>\s*\.detail-season-trigger\s*\{[^}]*display:\s*flex[^}]*justify-content:\s*center[^}]*width:\s*100%[^}]*height:\s*100%[^}]*cursor:\s*pointer/s);
  assert.match(styles, /\.detail-season-trigger-label\s*\{[^}]*pointer-events:\s*none/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-season-dropdown\s*>\s*\.detail-season-trigger\s*\{[^}]*background:\s*var\(--detail-command-surface\)\s*!important/s);
  assert.match(seasonPicker, /open=\{open\}/);
  assert.match(seasonPicker, /onOpenChange=\{handleOpenChange\}/);
  assert.match(seasonPicker, /const \[open, setOpen\] = useState\(false\)/);
  assert.doesNotMatch(styles, /\.detail-season-listbox\s*\{[^}]*min-width:\s*164px\s*!important/s);
  assert.doesNotMatch(styles, /\.detail-season-listbox\s*\{[^}]*width:\s*max-content\s*!important/s);
  assert.match(styles, /\.detail-season-listbox\s*\{[^}]*box-sizing:\s*border-box/s);
});


test('功能按钮使用统一图标盒 class 和 type="button"', () => {
  assert.match(detail, /className="detail-episode-tool-btn"/);
  assert.match(detail, /detail-episode-tool-btn detail-episode-view-btn \$\{effectiveEpisodeView === 'list' \? 'active' : ''\}/);
  assert.match(detail, /detail-episode-tool-btn detail-episode-view-btn \$\{effectiveEpisodeView === 'grid' \? 'active' : ''\}/);
  assert.match(detail, /type="button"/);
  assert.match(styles, /\.detail-episode-tool-btn\s*\{[^}]*width:\s*32px[^}]*height:\s*32px/s);
  assert.match(styles, /\.detail-episode-tool-btn svg\s*\{[^}]*width:\s*16px[^}]*height:\s*16px/s);
  assert.match(styles, /\.detail-episode-tool-btn svg\s*\{[^}]*stroke-width:\s*1\.9/s);
});


test('季度触发器使用统一中文字体并把文字几何居中', () => {
  assert.match(styles, /\.detail-episode-heading\s*\{[^}]*gap:\s*14px/s);
  assert.doesNotMatch(styles, /\.detail-episode-heading:has\(\.detail-season-dropdown\)/);
  assert.match(styles, /\.detail-season-dropdown\s*>\s*\.detail-season-trigger\s*\{[^}]*position:\s*relative/s);
  assert.match(styles, /\.detail-season-trigger-label\s*\{[^}]*display:\s*inline-flex[^}]*justify-content:\s*center/s);
  // 季度文字是普通文本节点，触发器和下拉选项共用同一套字体规则，不再逐字拆 glyph。
  assert.doesNotMatch(seasonPicker, /detail-season-label-glyph/);
  assert.doesNotMatch(styles, /\.detail-season-label-glyph\s*\{/);
  assert.match(styles, /--detail-command-font-size:\s*14px/);
  assert.match(styles, /--detail-command-font-weight:\s*600/);
  assert.match(styles, /--detail-command-line-height:\s*20px/);
  assert.match(styles, /\.detail-season-label\s*\{[^}]*font-size:\s*var\(--detail-command-font-size\)/s);
  assert.doesNotMatch(styles, /\.detail-season-trigger-label\s*\{[^}]*(?:font-size|font-weight|line-height):/s);
  assert.doesNotMatch(styles, /\.detail-season-option-label\s*\{[^}]*(?:font-size|font-weight|line-height):/s);
  assert.match(styles, /\.detail-season-listbox \[role='option'\]\s*\{[^}]*line-height:\s*var\(--detail-command-line-height\)/s);
  assert.match(seasonPicker, /normalizeSeasonLabel/);
  assert.match(seasonPicker, /parseChineseSeasonNumber/);
  assert.doesNotMatch(seasonPicker, /chineseDigits/);
  assert.match(styles, /\.detail-season-label\s*\{[^}]*font-family:\s*var\(--detail-command-font-family\)/s);
  assert.match(styles, /\.detail-season-listbox \[role='option'\]\s*\{[^}]*font-family:\s*var\(--detail-command-font-family\)/s);
});


test('三套主题命令层使用轻量 Acrylic 分组而不是独立黑色方块', () => {
  for (const theme of ['fluent', 'mica', 'cinema']) {
    const themeStart = styles.lastIndexOf(`:root[data-theme='${theme}'] {`);
    const themeBlock = styles.slice(themeStart, styles.indexOf('}', themeStart));
    assert.match(themeBlock, /--detail-command-surface:\s*rgb\(14 24 30 \/ \.44\)/);
    assert.match(themeBlock, /--detail-command-flyout:\s*rgb\(18 22 26 \/ \.90\)/);
    assert.match(themeBlock, /--detail-command-fg:\s*rgb\(248 250 252 \/ \.98\)/);
  }
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-toolbar\s*\{[^}]*padding:\s*3px[^}]*background:\s*var\(--detail-command-surface\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-tool-btn,[\s\S]*?background:\s*transparent/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-tool-btn:hover,[\s\S]*?background:\s*var\(--detail-command-hover\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-pager button:hover\s*\{[^}]*background:\s*var\(--detail-command-hover\)/s);
  assert.match(styles, /\.detail-page\.detail-classic-page \.episode-strip-slider\s*\{[^}]*border:\s*0[^}]*background:\s*transparent[^}]*box-shadow:\s*none/s);
});


test('命令栏只绘制一个分组表面，翻页器和子控件不叠加第二层框', () => {
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-heading\s*\{[^}]*background:\s*transparent\s*!important/s);
  assert.doesNotMatch(styles, /\.detail-page\.detail-classic-page \.detail-episode-head > div/);
  assert.match(styles, /\.detail-page\.detail-classic-page \.detail-episode-toolbar\s*\{[^}]*background:\s*var\(--detail-command-surface\)/s);
  assert.match(styles, /\.detail-episode-toolbar\s*\{[^}]*padding:\s*0[^}]*border:\s*0[^}]*background:\s*transparent/s);
  assert.match(styles, /\.detail-episode-pager\s*\{[^}]*padding:\s*0[^}]*border:\s*0[^}]*background:\s*transparent/s);
  assert.doesNotMatch(styles, /\.detail-episode-pager\s*\{[^}]*backdrop-filter/s);
  assert.doesNotMatch(styles, /\.detail-episode-pager\s*\{[^}]*box-shadow/s);
  assert.match(styles, /--detail-command-fg/s);
  assert.doesNotMatch(styles, /--detail-command-fg:\s*$/m);
  assert.doesNotMatch(styles, /--range-fill:\s*#(?:df5d4a|ff7a66|cf654f)/i);
});


test('浅色主题名称和底色准确表达雾蓝与纯白方向', () => {
  assert.match(settings, /name:\s*'雾蓝云母'/);
  assert.match(settings, /name:\s*'纯白'/);
  assert.doesNotMatch(settings, /云母石墨/);

  const themeAnchor = styles.indexOf('/* Final Fluent/Mica visual calibration');
  const micaStart = styles.indexOf(":root[data-theme='mica'] {\n", themeAnchor);
  const micaBlock = styles.slice(micaStart, styles.indexOf('}', micaStart));
  assert.match(micaBlock, /--app-bg:\s*#ffffff/);
  assert.match(micaBlock, /--mica-a:\s*#ffffff/);
  assert.match(micaBlock, /--mica-b:\s*#ffffff/);
});
