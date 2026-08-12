import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

test('本地目录解析使用独立的主操作按钮', () => {
  assert.match(page, /className="media-entry-parse-button"/);
  assert.match(page, /entry\.status === 'parsed' \? '重新解析' : '解析并继续'/);
  assert.match(styles, /\.media-entry-parse-button\.fui-Button\s*\{/);
});

test('唯一目录条目也可以删除并显示空状态', () => {
  assert.match(page, /const removeDirectoryEntry = \(entryId: string\) =>/);
  assert.match(page, /onClick=\{\(\) => removeDirectoryEntry\(entry\.id\)\}/);
  assert.doesNotMatch(page, /aria-label="删除目录条目"[^>]*disabled=\{entries\.length === 1\}/);
  assert.match(page, /entries\.length === 0 && <div className="media-directory-empty"/);
});

test('桌面目录表格限制路径与备注宽度并保留操作空间', () => {
  assert.match(
    styles,
    /grid-template-columns:\s*34px minmax\(280px,\s*620px\) minmax\(120px,\s*220px\) 96px 156px;/,
  );
  assert.match(
    styles,
    /@container media-flow \(max-width: 1420px\)[\s\S]*grid-template-columns:\s*30px minmax\(280px,\s*560px\) minmax\(120px,\s*200px\) 96px 156px !important;/,
  );
});

test('自动刮削完成后可以从完成卡片导入新目录', () => {
  assert.match(page, /const beginNewImport = \(\) =>/);
  assert.match(
    page,
    /const beginNewImport = \(\) => \{[\s\S]*setEntries\(\[entry\]\);[\s\S]*setTask\(null\);[\s\S]*setImportModeActive\(true\);[\s\S]*setStep\('import'\);[\s\S]*\};/,
  );
  assert.match(page, /onNewImport=\{beginNewImport\}/);
  const workbench = readFileSync(
    new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url),
    'utf8',
  );
  assert.match(workbench, /mode === 'scrape' && task\?\.status === 'succeeded' && onNewImport/);
  assert.match(workbench, />导入新目录<\/Button>/);
  assert.match(
    styles,
    /\.media-new-import-command\.fui-Button\s*\{[^}]*margin-right:\s*auto;/s,
  );
});
