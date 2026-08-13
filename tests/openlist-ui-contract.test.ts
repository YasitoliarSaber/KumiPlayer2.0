import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const mediaPage = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const settingsPage = readFileSync(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8');
const openlistApi = readFileSync(new URL('../src/api/openlist.ts', import.meta.url), 'utf8');
const mediaWorkflow = readFileSync(new URL('../src/stores/mediaWorkflow.ts', import.meta.url), 'utf8');
const backgroundImportStatus = readFileSync(new URL('../src/components/media/MediaBackgroundImportStatus.tsx', import.meta.url), 'utf8');
const types = readFileSync(new URL('../src/api/types.ts', import.meta.url), 'utf8');
const backendOpenlistApi = readFileSync(new URL('../backend/app/api/openlist.py', import.meta.url), 'utf8');
const backendClient = readFileSync(new URL('../backend/app/integrations/openlist/client.py', import.meta.url), 'utf8');
const uiStore = readFileSync(new URL('../src/stores/ui.ts', import.meta.url), 'utf8');
const titleBar = readFileSync(new URL('../src/components/shell/DesktopTitleBar.tsx', import.meta.url), 'utf8');
const workDetailPage = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
const maintenancePanel = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');
const libraryStore = readFileSync(new URL('../src/stores/library.ts', import.meta.url), 'utf8');
const tasksDbPy = readFileSync(new URL('../backend/app/db/tasks.py', import.meta.url), 'utf8');
const libraryService = readFileSync(new URL('../backend/app/library/service.py', import.meta.url), 'utf8');
const libraryStorePy = readFileSync(new URL('../backend/app/library/store.py', import.meta.url), 'utf8');

test('媒体管理页提供 OpenList 连接来源入口（不再是“夸克试点”单来源）', () => {
  assert.match(mediaPage, /\{ value: 'openlist', label: 'OpenList 连接' \}/);
  assert.match(mediaPage, /浏览 OpenList 远端目录并选择多个目录批量导入/);
  assert.match(mediaPage, /media-openlist-browser/);
});

test('OpenList 未连接时显示前往设置的明确引导', () => {
  assert.match(mediaPage, /尚未配置 OpenList 连接/);
  assert.match(mediaPage, /请先到设置页填写 OpenList 服务地址、账号、远端映射根与本地挂载根路径/);
  assert.match(mediaPage, /onClick=\{\(\) => goSettings\(\)\}>前往设置/);
});

test('目录浏览器提供面包屑、上级、懒加载、缓存状态与显式刷新', () => {
  assert.match(mediaPage, /media-openlist-crumbs/);
  assert.match(mediaPage, /aria-label="远端目录面包屑"/);
  assert.match(mediaPage, /openlistParentPath/);
  assert.match(mediaPage, />上级</);
  assert.match(mediaPage, /media-openlist-entries/);
  assert.match(mediaPage, /此目录为空/);
  // 显式强制刷新当前层（refresh=true），并展示缓存状态
  assert.match(mediaPage, />强制刷新当前层</);
  assert.match(mediaPage, /browseOpenlist\(openlistPathRef\.current, true\)/);
  assert.match(mediaPage, /openlistCacheMeta/);
  assert.match(mediaPage, /缓存数据 · 正在后台更新/);
});

test('目录复选框选择与名称导航互不冲突，支持选择篮与父子去重', () => {
  // 复选框独立于导航按钮，点击复选框只选择不进入目录
  assert.match(mediaPage, /<label className="media-openlist-select"/);
  assert.match(mediaPage, /toggleOpenlistSelection\(entry\)/);
  assert.match(mediaPage, /onClick=\{\(\) => void browseOpenlist\(entry\.remote_path\)\}/);
  // 选择篮跨层保留、父子去重、数量上限
  assert.match(mediaPage, /media-openlist-basket/);
  assert.match(mediaPage, /选择篮（\{openlistSelection\.length\}\/\{OPENLIST_BATCH_LIMIT\}）/) || assert.match(mediaPage, /OPENLIST_BATCH_LIMIT/);
  assert.match(mediaPage, /openlistIsAncestorOrSelf/);
  assert.match(mediaPage, /该目录已被已选的父目录包含/);
  assert.match(mediaPage, /removeOpenlistSelection/);
});

test('批量导入入口、进度汇总与恢复兼容', () => {
  // V2 durable batch：入口直接走 createImportBatch（旧 batchImport 已不再由页面调用）
  assert.match(mediaPage, /openlistApi\.createImportBatch/);
  assert.match(mediaPage, /批量导入 \{openlistSelection\.length\} 个目录/);
  // 批量导入的持久化任务由 discovery_scan 类型承载（旧 openlist_batch_import 已移除）
  assert.match(mediaPage, /task_type: 'discovery_scan'/);
  assert.match(mediaPage, /batch_summary/);
  // OpenList 批次直接进入后台观察页，绝不回流旧确认工作台。
  assert.match(mediaPage, /setBackgroundImport\(\{ source: 'openlist', batchId: batch\.batch_id \}\)/);
  assert.match(mediaPage, /setStep\('background'\)/);
});

test('选中后进入后台扫描并显示进度与取消', () => {
  // V2 durable batch：选中后创建持久批次进入后台扫描（旧 importRemote 单目录链路已替换）
  assert.match(mediaPage, /openlistApi\.createImportBatch/);
  assert.match(mediaPage, /正在扫描远端目录/);
  assert.match(mediaPage, /个目录<\/dd>/);
  assert.match(mediaPage, /cancelOpenlistScan/);
  assert.match(mediaPage, />取消</);
  // 状态持续关联至下游 mirror/scrape，不逐项读取旧确认预览。
  assert.match(mediaPage, /batchHasActiveWork/);
  assert.match(mediaPage, /MediaBackgroundImportStatus/);
  assert.doesNotMatch(mediaPage, /importsApi\.getPreview\('openlist', planId\)/);
});

test('OpenList 导入层不使用 WebDAV 或直链播放文案', () => {
  assert.doesNotMatch(mediaPage, /webdav|WebDAV|直链播放|dav\//i);
  assert.doesNotMatch(openlistApi, /webdav|WebDAV|直链播放|dav\//i);
  assert.match(backendOpenlistApi, /normalize_openlist_server_url/);
  assert.match(backendClient, /normalize_openlist_server_url/);
  assert.match(backendClient, /_LOGIN_PATH = "\/api\/auth\/login"/);
});

test('设置页用一组字段保存 OpenList 连接，并派生 WebDAV 挂载地址', () => {
  assert.match(settingsPage, /OpenList 连接</);
  assert.match(settingsPage, /OpenList 是取得目录的连接\/导入方式，不是内容提供商/);
  assert.match(settingsPage, /OpenList 地址/);
  assert.match(settingsPage, /buildOpenListWebdavAddress/);
  assert.match(settingsPage, /挂载地址/);
  assert.match(settingsPage, /远端总根路径/);
  assert.match(settingsPage, /本地总挂载根路径/);
  assert.match(settingsPage, /浏览缓存时长（分钟）/);
  assert.match(settingsPage, /预取直接子目录数（上限 50）/);
  assert.match(settingsPage, /OpenList 用户名/);
  assert.match(settingsPage, /OpenList 密码/);
  assert.match(settingsPage, /保存连接/);
  assert.doesNotMatch(settingsPage, /ConfigRow label="OpenList/);
  assert.match(settingsPage, /openlist_configured/);
  assert.match(settingsPage, /已保存；留空即可继续使用/);
  assert.match(settingsPage, />测试连接</);
  assert.match(settingsPage, /openlistApi\.saveConfig/);
  assert.match(settingsPage, /\.\.\.openlistDraft/);
  assert.match(settingsPage, /openlistApi\.testConnection\(\{[\s\S]*server_url: openlistDraft\.server_url/);
  assert.doesNotMatch(settingsPage, /testConnection\(\{\s*\.\.\.openlistDraft/);
  assert.match(mediaPage, /openlistScanTask\?\.status === 'failed'/);
  assert.match(mediaPage, /OpenList 扫描失败<\/strong>/);
  assert.match(mediaPage, /openlistScanTask\.error \|\| openlistScanTask\.message/);
  // 凭据零泄露：设置页不读取 config 中的用户名/密码字段
  assert.doesNotMatch(settingsPage, /config\.openlist_username/);
  assert.doesNotMatch(settingsPage, /config\.openlist_password/);
});

test('设置页来源目录路由不重复要求填写连接字段与挂载根', () => {
  assert.match(settingsPage, /来源目录路由/);
  assert.match(settingsPage, /readOnly className="settings-input"/);
  assert.match(settingsPage, /推导本地路径（只读）/);
  assert.match(settingsPage, /不作为媒体来源时取消勾选/);
  assert.match(settingsPage, /openlistApi\.discoverRoutes/);
  assert.match(settingsPage, /openlistApi\.saveRoutes/);
  // 路由区只读预览：不重复出现连接级字段输入
  assert.doesNotMatch(settingsPage, /<strong>来源目录路由<\/strong>[\s\S]*value=\{openlistDraft\.server_url\}/);
  assert.doesNotMatch(settingsPage, /<strong>来源目录路由<\/strong>[\s\S]*openlistApi\.testConnection/);
});

test('公共配置类型只暴露 openlist_configured，不携带凭据字段', () => {
  const configTypes = readFileSync(new URL('../src/api/config.ts', import.meta.url), 'utf8');
  assert.match(configTypes, /openlist_configured: boolean/);
  assert.doesNotMatch(configTypes, /openlist_username: string/);
  assert.doesNotMatch(configTypes, /openlist_password: string/);
});

test('OpenList 前端 API 覆盖浏览、路由、预取与 durable 批量导入', () => {
  assert.match(openlistApi, /testConnection:/);
  assert.match(openlistApi, /saveConfig:/);
  assert.match(openlistApi, /browse:/);
  assert.match(openlistApi, /rescanPreset:/);
  assert.match(openlistApi, /prefetch:/);
  assert.match(openlistApi, /getRoutes:/);
  assert.match(openlistApi, /discoverRoutes:/);
  assert.match(openlistApi, /saveRoutes:/);
  // V2 durable batch 链路（C2 起旧 /import 与 /batch-import 已退役）
  assert.match(openlistApi, /createImportBatch:/);
  assert.match(openlistApi, /getImportBatch:/);
  assert.match(openlistApi, /cancelImportBatch:/);
  assert.doesNotMatch(openlistApi, /importRemote:/);
  assert.doesNotMatch(openlistApi, /batchImport:/);
  assert.doesNotMatch(openlistApi, /\/api\/openlist\/batch-import/);
  assert.match(openlistApi, /\/api\/openlist\/browse/);
  assert.match(openlistApi, /\/api\/openlist\/import-batch/);
  assert.match(openlistApi, /\/api\/openlist\/import-batches/);
  assert.match(openlistApi, /\$\{refresh \? 'true' : 'false'\}/);
  assert.match(openlistApi, /\/api\/openlist\/routes/);
});

test('来源类型体系已扩展到 openlist', () => {
  assert.match(types, /ImportSourceId = 'pan115' \| 'baidu' \| 'local' \| 'openlist'/);
  assert.match(mediaWorkflow, /MediaWorkflowSource = 'pan115' \| 'baidu' \| 'local' \| 'openlist'/);
  assert.match(uiStore, /SourceId = 'all' \| 'pan115' \| 'baidu' \| 'local' \| 'openlist'/);
  assert.match(types, /source: 'pan115' \| 'baidu' \| 'local' \| 'openlist'/);
  assert.match(types, /sources\?: Array<'pan115' \| 'baidu' \| 'local' \| 'openlist'>/);
});

test('页面重新进入时会恢复活动 OpenList 扫描任务', () => {
  // 恢复入口：source 为 openlist 且已配置时按来源+任务类型（discovery_scan）查询
  assert.match(mediaPage, /tasksApi\.list\(\{ source: 'openlist', task_type: 'discovery_scan', limit: 100 \}\)/);
  assert.match(mediaPage, /recoverOpenlistTask/);
  assert.match(mediaPage, /已恢复后台目录扫描/);
  // 恢复逻辑不重新提交导入请求（既不创建批次也不调旧 importRemote）
  const recoverBlock = mediaPage.match(/const recoverOpenlistTask = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || '';
  assert.doesNotMatch(recoverBlock, /importRemote/);
  assert.doesNotMatch(recoverBlock, /createImportBatch/);
  // 恢复只接管 pending / running，成功不因刷新自动跳转
  assert.match(mediaPage, /status === 'pending' \|\| task\.status === 'running'/);
  // v2 批次终态恢复：已完成的 durable batch 按 batch_id 读回并接管，恢复确认条目
  assert.match(mediaPage, /openlistApi\.getImportBatch\(openlistBatchIdRef\.current\)/);
  assert.match(mediaPage, /attachOpenlistBatch\(updated\)/);
});

test('重复提交按连接互斥直接创建持久批次接管，不展示内部任务 ID', () => {
  // v2 durable batch 按连接互斥：不再有“并发错误后降级接管”文案，创建直接走 createImportBatch
  assert.doesNotMatch(mediaPage, /同一来源已有 openlist_import 任务运行中/);
  assert.match(mediaPage, /openlistApi\.createImportBatch/);
  // 创建与恢复共用 attachOpenlistBatch 接管批次：记录 batch_id、轮询批次状态
  assert.match(mediaPage, /attachOpenlistBatch/);
  assert.match(mediaPage, /openlistBatchIdRef\.current = batch\.batch_id/);
  // 后台页直接保留 batch 身份，并持续轮询下游状态。
  assert.match(mediaPage, /setBackgroundImport\(\{ source: 'openlist', batchId: batch\.batch_id \}\)/);
  assert.match(mediaPage, /batchHasActiveWork\(updated\)/);
});

test('扫描进度卡展示目录、计数与真实总量语义', () => {
  assert.match(mediaPage, /found_directory_count/);
  assert.match(mediaPage, /found_file_count/);
  assert.match(mediaPage, /found_entry_count/);
  assert.match(mediaPage, /found_video_candidate_count/);
  assert.match(mediaPage, /scanned_directory_count/);
  assert.match(mediaPage, /queued_directory_count/);
  assert.match(mediaPage, /current_directory_total/);
  assert.match(mediaPage, /current_directory_collected/);
  assert.match(mediaPage, /整个目录树总量仍在统计中/);
  assert.match(mediaPage, /overall_total_known/);
  // 不伪造整体完成百分比
  assert.doesNotMatch(mediaPage, /已完成 [\d.]+%/);
  // 有服务端总数时显示 X / Y，否则显示第 N 页
  assert.match(mediaPage, /\$\{openlistScanResult\.current_directory_collected \?\? 0\} \/ \$\{openlistScanResult\.current_directory_total\} 项/);
  assert.match(mediaPage, /正在读取第 \$\{openlistScanResult\.current_page \?\? 1\} 页/);
  // 阶段式文字（写清单 / 验证挂载 / 构建计划）
  assert.match(mediaPage, /正在验证本地挂载文件/);
  assert.match(mediaPage, /正在识别作品并生成导入计划/);
});

test('失败、取消、完成三种终态都有独立可读 UI', () => {
  assert.match(mediaPage, /OpenList 扫描失败<\/strong>/);
  assert.match(mediaPage, />重试</);
  assert.match(mediaPage, />刷新任务状态</);
  assert.match(mediaPage, /扫描已停止<\/strong>/);
  assert.match(mediaPage, /已保留原有媒体库，不会生成半成品/);
  assert.match(mediaPage, />重新扫描</);
  // 终态与待处理项都在后台观察页展示。
  assert.match(mediaPage, /MediaBackgroundImportStatus/);
  assert.match(backgroundImportStatus, /媒体库已更新/);
  assert.match(backgroundImportStatus, /media-workbench-substages media-background-stages/);
});

test('顶部来源筛选与详情页来源标签使用统一 OpenList 名称', () => {
  assert.match(titleBar, /\{ value: 'openlist', label: 'OpenList 连接' \}/);
  assert.match(workDetailPage, /openlist: 'OpenList 连接'/);
  assert.match(workDetailPage, /priority = \{ pan115: 0, baidu: 1, local: 2, openlist: 3 \}/);
  assert.match(maintenancePanel, /openlist: 'OpenList 连接'/);
  // 媒体库预设来源标签按 provider 显示（不再统一显示为 OpenList）
  assert.match(mediaPage, /openlistProviderLabel\(preset\.provider_id\)/);
  assert.match(mediaPage, /providerLabels/);
  assert.match(mediaPage, /quark: '夸克网盘'/);
});

test('详情页来源标签优先显示真实 provider，OpenList 仅作辅助回退', () => {
  // provider 优先：quark/pan115/baidu 直接显示内容提供商
  assert.match(workDetailPage, /providerDisplayLabel/);
  assert.match(workDetailPage, /quark: '夸克网盘'/);
  assert.match(workDetailPage, /workSourceLabel\(source, work\)/);
  assert.match(workDetailPage, /episodeSourceLabel\(episode\.source, episode\)/);
  // provider 存在时不显示 “OpenList 连接”
  assert.match(workDetailPage, /if \(provider\) return provider/);
  // 前端数据类型携带 provider 贯通字段
  assert.match(types, /provider_id\?: ProviderId/);
  assert.match(types, /ingest_method\?: IngestMethod/);
  assert.match(types, /source_route_id\?: string/);
});

test('批量任务取消/失败后保持 durable 批次观察而不回退确认页', () => {
  assert.match(openlistApi, /units\?: BackgroundImportUnit\[\]/);
  assert.match(mediaPage, /MediaBackgroundImportStatus/);
  assert.match(mediaPage, /batchHasActiveWork/);
  assert.match(backgroundImportStatus, /待处理识别结果/);
  // 取消走 durable batch 取消端点（API 层定义），页面通过 cancelImportBatch 调用
  assert.match(openlistApi, /import-batches\/\$\{batchId\}\/cancel/);
  assert.match(mediaPage, /openlistApi\.cancelImportBatch/);
});

test('Library API 序列化输出 provider 字段（数据能到达前端）', () => {
  // _work_to_dict 输出 provider 三字段
  assert.match(libraryService, /"provider_id": w\.provider_id/);
  assert.match(libraryService, /"ingest_method": w\.ingest_method/);
  assert.match(libraryService, /"source_route_id": w\.source_route_id/);
  // episode 序列化输出 provider_id
  assert.match(libraryService, /"provider_id": e\.provider_id/);
  // library_index.json 反序列化读回 provider 三字段（重启后不丢）
  assert.match(libraryStorePy, /provider_id=w\.get\("provider_id", ""\)/);
  assert.match(libraryStorePy, /ingest_method=w\.get\("ingest_method", ""\)/);
  assert.match(libraryStorePy, /source_route_id=w\.get\("source_route_id", ""\)/);
});

test('浏览竞争防护：只有最新请求提交目录状态', () => {
  assert.match(mediaPage, /openlistBrowseSeqRef/);
  assert.match(mediaPage, /seq !== openlistBrowseSeqRef\.current/);
  assert.match(mediaPage, /离开\/切换来源：使旧浏览请求与旧预取 generation 失效/);
  assert.match(mediaPage, /openlistApi\.prefetch\(\[\]\)/);
});

test('来源筛选按作品来源列表过滤（跨来源合并作品可见）', () => {
  assert.match(libraryStore, /w\.source === activeSource \|\| w\.sources\?\.includes\(activeSource\) === true/);
});

test('OpenList 首发禁用新番追更，并说明原因', () => {
  assert.match(mediaPage, /value="seasonal" disabled=\{source === 'openlist'\}/);
  assert.match(mediaPage, /首发暂不支持 OpenList 自动追更/);
});

test('后端重启后遗留任务收口为可读失败，错误信息指向重新扫描', () => {
  assert.match(tasksDbPy, /后端已重启，扫描未完成。请重新扫描该文件夹/);
});
