import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');

test('作品详情只消费 V4 作品、季度和 Asset 身份', () => {
  assert.match(detail, /getWorkDetail\(selectedWorkId\)/);
  assert.match(detail, /episode\.assets\?\.length/);
  assert.match(detail, /playbackApi\.play/);
  assert.doesNotMatch(detail, /plan_id|import_plan_id|scrapeApi|trackingApi|scrape_target_id/);
});

test('作品详情不会在前端重新合并同一作品或重写集号', () => {
  assert.doesNotMatch(detail, /canonical_work_id|series_group|setEpisodeNumber|merge.*episode/i);
  assert.match(detail, /Number\(episode\.season_number\)/);
});
