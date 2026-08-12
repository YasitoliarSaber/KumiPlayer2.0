import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('媒体库清理预览和结果明确报告追更控制记录', () => {
  const api = readFileSync(new URL('../src/api/library.ts', import.meta.url), 'utf8');
  const maintenance = readFileSync(new URL('../src/components/media/LibraryMaintenancePanel.tsx', import.meta.url), 'utf8');

  assert.match(api, /tracking_binding_count:\s*number/);
  assert.match(api, /library_work_count:\s*number/);
  assert.match(api, /tracking_scan_run_count:\s*number/);
  assert.match(api, /deleted_tracking_binding_count:\s*number/);
  assert.match(api, /deleted_library_work_count:\s*number/);
  assert.match(api, /deleted_tracking_scan_run_count:\s*number/);
  assert.match(maintenance, /preview\.tracking_binding_count/);
  assert.match(maintenance, /preview\.library_work_count/);
  assert.match(maintenance, /部媒体库作品/);
  assert.match(maintenance, /追更记录/);
  assert.match(maintenance, /result\.deleted_tracking_binding_count/);
});

test('打开新番页只读取追更状态，创建和扫描必须由按钮显式触发', () => {
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');

  assert.match(
    category,
    /useEffect\(\(\) => \{\s*if \(activeCategory === 'seasonal'\) void loadTrackingBindings\(\);\s*\}, \[activeCategory\]\)/,
  );
  assert.match(category, /onClick=\{scanAllTracking\}/);
  assert.match(category, /onClick=\{addTracking\}/);
  assert.match(category, /扫描更新当前/);
  assert.doesNotMatch(category, /retryTracking/);
  assert.doesNotMatch(category, /重新自动处理|打开作品|查看处理详情/);
  assert.doesNotMatch(
    category,
    /useEffect\(\(\) => \{[^}]*trackingApi\.(?:create|scan|scanAll)/s,
  );
});
