import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const client = readFileSync(new URL('../src/api/mediaPresets.ts', import.meta.url), 'utf8');

test('本地媒体库卡片按实际路径展示并使用重新扫描操作', () => {
  assert.match(page, /preset\.source === 'local' \? '本地目录'/);
  assert.match(page, /preset\.update_mode === 'local_scan'/);
  assert.match(page, /重新扫描本地目录/);
  assert.match(page, /sourcesApi\.createLocalImportBatch\(/);
  assert.match(page, /setBackgroundImport\(\{ source: 'local', batchId: batch\.batch_id \}\)/);
  assert.match(page, /preset\.update_mode === 'directory_tree'[\s\S]*导入新版并安全比对/);
});

test('媒体库客户端区分目录树比对和本地路径扫描', () => {
  assert.match(client, /source: 'pan115' \| 'baidu' \| 'local' \| 'openlist'/);
  assert.match(client, /update_mode: 'directory_tree' \| 'local_scan'/);
  assert.match(client, /rescanLocal:/);
  assert.match(client, /\/rescan-local/);
});
