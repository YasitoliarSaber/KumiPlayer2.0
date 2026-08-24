/** P-004 10.7 item10 / P-005 11.9 item15：静态证明新组件不复活 Legacy 实现。 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const sources = [
  'src/pages/MediaManagementPage.tsx',
  'src/components/media/V4RecognitionSummary.tsx',
  'src/components/media/V4ExecutionProgress.tsx',
  'src/components/media/LibraryMaintenancePanel.tsx',
  'src/components/media/MediaProviderIcon.tsx',
  'src/components/media/OpenListFolderBrowser.tsx',
  'src/lib/mediaSummary.ts',
  'src/utils/libraryCategories.ts',
  'src/api/workDetailV4Compatibility.ts',
];

const legacyMarkers = [
  'ImportPlan',
  'MediaPreset',
  'import_plan_id',
  'mirrorApi.generate',
  'scrapeApi.autoScrape',
  'BackgroundImportUnit',
  'TaskRecord',
  'LibraryDeleteSource',
  'canonical_work_id',
];

for (const file of sources) {
  const text = readFileSync(new URL(`../${file}`, import.meta.url), 'utf8');
  test(`${file} 不复活 Legacy 标识`, () => {
    for (const marker of legacyMarkers) {
      assert.ok(!text.includes(marker), `${file} 不应包含 ${marker}`);
    }
  });
}
