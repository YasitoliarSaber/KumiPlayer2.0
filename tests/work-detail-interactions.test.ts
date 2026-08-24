import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
const compatibility = readFileSync(new URL('../src/api/workDetailV4Compatibility.ts', import.meta.url), 'utf8');

test('作品详情只消费 V4 作品、季度和 Asset 身份', () => {
  assert.match(detail, /getWorkDetail\(selectedWorkId\)/);
  assert.match(detail, /targetEpisode\?\.asset_id/);
  assert.match(detail, /playbackApi\.play/);
  assert.match(detail, /workDetailV4Capabilities/);
  assert.doesNotMatch(compatibility, /\/api\/(scrape|tracking)|import_plan_id/);
  assert.match(compatibility, /manualScrape: true/);
  assert.match(compatibility, /titleMutation: true/);
  assert.match(compatibility, /workDeletion: true/);
});

test('作品详情恢复旧版沉浸式结构而不是简化信息列表', () => {
  assert.match(detail, /detail-page detail-classic-page/);
  assert.match(detail, /detail-hero/);
  assert.match(detail, /detail-content-drawer/);
  assert.match(detail, /DetailSeasonPicker/);
  assert.match(detail, /detail-episode-section/);
  assert.match(detail, /episode-source-badge/);
  assert.doesNotMatch(detail, /work-detail-summary|work-detail-episode-list/);
});

test('作品详情不会在前端重新合并同一作品或重写集号', () => {
  assert.doesNotMatch(detail, /canonical_work_id|setEpisodeNumber|episode_number\s*=/i);
  assert.match(detail, /episode\.season_number === selectedSeason\.season_number/);
});
