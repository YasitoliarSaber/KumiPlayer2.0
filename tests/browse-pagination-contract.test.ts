// 模块4 C3 契约测试：OpenList browse 真分页（一次只拉一页，loadMore 追加）
// 断言方式与 openlist-ui-contract.test.ts 一致：node:test 静态契约（读源码断言）
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const openlistApiSource = readFileSync(new URL('../src/api/openlist.ts', import.meta.url), 'utf8');
const mediaPage = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');

test('browse 默认发送 page=1&per_page=100，并保留兼容签名', () => {
  // 签名兼容：browse(path) / browse(path, 1, true) 仍可用（perPage 默认 100）
  assert.match(openlistApiSource, /browse: \(path = '', page = 1, refresh = false, perPage = 100\)/);
  // 真正发送 page / per_page / refresh 参数
  assert.match(openlistApiSource, /page=\$\{page\}&per_page=\$\{perPage\}&refresh=/);
  assert.match(openlistApiSource, /\$\{refresh \? 'true' : 'false'\}/);
});

test('OpenListBrowseResult 携带分页字段 page/per_page/has_more（total 已知），truncated 不再是必需字段', () => {
  const resultBlock = openlistApiSource.match(/interface OpenListBrowseResult \{[\s\S]*?\n\}/)?.[0] || '';
  assert.match(resultBlock, /page: number/);
  assert.match(resultBlock, /per_page: number/);
  assert.match(resultBlock, /total: number/);
  assert.match(resultBlock, /has_more: boolean/);
  assert.match(resultBlock, /truncated\?: boolean/);
});

test('进入目录 / 刷新都重置 page=1 并 replace entries（不追加）', () => {
  const browseBlock = mediaPage.match(/const browseOpenlist = async \(path: string, refresh = false\) => \{[\s\S]*?\n  \};/)?.[0] || '';
  assert.ok(browseBlock.length > 0, 'browseOpenlist 存在');
  // 进入目录与强制刷新统一走 browse(path, 1, refresh)：page 恒为 1
  assert.match(browseBlock, /const result = await openlistApi\.browse\(path, 1, refresh\);/);
  // replace：直接整体覆盖 entries，而不是展开追加
  assert.match(browseBlock, /setOpenlistEntries\(result\.entries\);/);
  assert.doesNotMatch(browseBlock, /\.\.\.result\.entries/);
  // 重置分页状态
  assert.match(browseBlock, /setOpenlistCurrentPage\(result\.page > 0 \? result\.page : 1\);/);
  assert.match(browseBlock, /setOpenlistHasMore\(Boolean\(result\.has_more\)\);/);
  assert.match(browseBlock, /setOpenlistTotalCount\(result\.total \|\| 0\);/);
});

test('loadMore 只拉下一页并 append，绝无 while(has_more) 自动循环拉页', () => {
  const loadMoreBlock = mediaPage.match(/const loadMoreOpenlist = async \(\) => \{[\s\S]*?\n  \};/)?.[0] || '';
  assert.ok(loadMoreBlock.length > 0, 'loadMoreOpenlist 存在');
  // 一次只拉一页：page = 当前页 + 1
  assert.match(loadMoreBlock, /const nextPage = openlistCurrentPage \+ 1;/);
  assert.match(loadMoreBlock, /openlistApi\.browse\(openlistPathRef\.current, nextPage, false\)/);
  // append：以当前 entries 为基底追加（按 remote_path 轻量去重，纯 UI 防御）
  assert.match(loadMoreBlock, /setOpenlistEntries\(\(current\) => \{/);
  assert.match(loadMoreBlock, /const seen = new Set\(current\.map\(\(entry\) => entry\.remote_path\)\);/);
  assert.match(loadMoreBlock, /return \[\.\.\.current, \.\.\.fresh\];/);
  // 防重复点击：loading 中不再发请求
  assert.match(loadMoreBlock, /if \(openlistLoadingMore \|\| !openlistHasMore\) return;/);
  // 绝无 while(has_more) 自动拉完所有页：loadMore 块内没有 while 循环、has_more 只出现一次
  assert.doesNotMatch(loadMoreBlock, /while\s*\(/);
  assert.doesNotMatch(loadMoreBlock, /has_more[\s\S]*has_more/);
});

test('整个浏览链路不存在 while(has_more) 自动拉页循环', () => {
  assert.doesNotMatch(mediaPage, /while\s*\([^)]*has_more/);
  assert.doesNotMatch(mediaPage, /while\s*\([^)]*openlistHasMore/);
});

test('UI 显示已加载计数（总数已知 X / Y，未知 X 项）与加载更多按钮', () => {
  assert.match(mediaPage, /已加载 \$\{openlistEntries\.length\} 项/);
  assert.match(mediaPage, /已加载 \$\{openlistEntries\.length\} \/ \$\{openlistTotalCount\} 项/);
  assert.match(mediaPage, />\s*\{openlistLoadingMore \? '正在加载…' : '加载更多'\}\s*</);
  assert.match(mediaPage, /openlistHasMore &&/);
  assert.match(mediaPage, /disabled=\{openlistLoadingMore\}/);
});
