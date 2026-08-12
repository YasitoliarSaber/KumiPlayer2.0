import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const list = readFileSync(new URL('../src/components/media/MediaLogList.tsx', import.meta.url), 'utf8');
const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const workbench = readFileSync(new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url), 'utf8');

test('解析摘要与执行活动使用正确列数且没有嵌套滚动', () => {
  // 确认页只展示 parse_logs，不混入历史任务日志
  assert.match(page, /logs=\{preview\.parse_logs\}/);
  assert.doesNotMatch(page, /logs=\{\[\.\.\.\(preview\?\.parse_logs[\s\S]*taskLogs/);
  // 无时间的解析摘要两列，有时间的执行活动三列
  assert.match(list, /log\.time \? 'has-timestamp' : 'no-timestamp'/);
  assert.match(styles, /\.media-log-item\.no-timestamp\s*\{[^}]*grid-template-columns:\s*20px minmax\(0, 1fr\)/s);
  assert.match(styles, /\.media-log-item\.has-timestamp\s*\{[^}]*grid-template-columns:\s*56px 20px minmax\(0, 1fr\)/s);
  // 普通日志列表不得出现内嵌滚动条
  assert.doesNotMatch(styles, /\.media-log-details ol\s*\{[\s\S]{0,200}?overflow-y:\s*auto/);
  assert.doesNotMatch(styles, /\.media-log-panel ol\s*\{[\s\S]{0,200}?overflow-y:\s*auto/);
  // 超过 limit 时可双向展开/收起
  assert.match(list, /hasOverflow/);
  assert.match(list, /查看全部/);
  assert.match(list, /仅显示最近/);
  // 工作台执行活动复用同一日志组件
  assert.match(workbench, /<MediaLogList/);
  assert.match(workbench, /ariaLabel="执行记录"/);
});
