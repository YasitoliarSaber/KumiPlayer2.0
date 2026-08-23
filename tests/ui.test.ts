import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { useUiStore } from '../src/stores/ui.ts';

test('作品详情导航会清除搜索状态', () => {
  const store = useUiStore.getState();
  store.setQuery('石纪元');
  store.goDetail('work-dr-stone');

  const state = useUiStore.getState();
  assert.equal(state.page, 'detail');
  assert.equal(state.selectedWorkId, 'work-dr-stone');
  assert.equal(state.query, '');
});

test('媒体管理页面只使用 V4 revision/job/work 合同', () => {
  const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
  const client = readFileSync(new URL('../src/api/mediaV4.ts', import.meta.url), 'utf8');

  assert.match(page, /mediaV4Api\.scan/);
  assert.match(page, /mediaV4Api\.preview/);
  assert.match(page, /mediaV4Api\.confirm/);
  assert.match(page, /revisionId/);
  assert.match(page, /jobs/);
  assert.doesNotMatch(page, /importsApi|mediaPresetsApi|execution_mode|plan_id/);
  assert.match(client, /\/api\/v4\/sources\/scan/);
  assert.match(client, /\/api\/v4\/imports\/preview/);
  assert.match(client, /\/api\/v4\/imports\/\$\{encodeURIComponent\(revisionId\)\}\/confirm/);
});

test('全局 TXT 拖放仍只进入 V4 媒体导入页', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const fileDrop = readFileSync(new URL('../src/platform/fileDrop.ts', import.meta.url), 'utf8');
  const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');

  assert.match(app, /listenForTreeFileDrop/);
  assert.match(app, /queueDroppedTreePath/);
  assert.match(app, /goManage\(\)/);
  assert.match(fileDrop, /extension === 'txt'/);
  assert.match(page, /consumeDroppedTreePath/);
  assert.doesNotMatch(page, /DroppedTreeDialog|importsApi|mediaPresetsApi/);
});

test('低频页面保持按需加载且不显示旧准备中遮罩', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /lazy\(\(\) => import\('\.\/pages\/MediaManagementPage'\)\)/);
  assert.match(app, /<Suspense fallback=\{null\}>/);
  assert.doesNotMatch(app, /正在准备页面|正在载入界面组件/);
});
