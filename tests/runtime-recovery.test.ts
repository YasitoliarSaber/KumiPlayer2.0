import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('启动失败与 React 渲染异常共享可操作的恢复界面', () => {
  const main = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
  const boundary = readFileSync(new URL('../src/components/errors/AppErrorBoundary.tsx', import.meta.url), 'utf8');
  const recovery = readFileSync(new URL('../src/components/errors/RecoveryView.tsx', import.meta.url), 'utf8');

  assert.match(main, /<AppErrorBoundary>/);
  assert.match(main, /<RecoveryView/);
  assert.match(main, /catch[\s\S]*?kumi-boot-splash[\s\S]*?\.remove\(\)[\s\S]*?createRoot\(root\)\.render/);
  assert.match(boundary, /getDerivedStateFromError/);
  assert.match(boundary, /componentDidCatch/);
  assert.match(recovery, /重新加载/);
  assert.match(recovery, /重启后端/);
  assert.match(recovery, /打开日志/);
  assert.match(recovery, /导出诊断/);
});

test('桌面平台适配只使用 Tauri 公开 API', () => {
  const titlebar = readFileSync(new URL('../src/components/shell/DesktopTitleBar.tsx', import.meta.url), 'utf8');
  const folderPicker = readFileSync(new URL('../src/platform/folderPicker.ts', import.meta.url), 'utf8');
  const fileDrop = readFileSync(new URL('../src/platform/fileDrop.ts', import.meta.url), 'utf8');
  const sources = [titlebar, folderPicker, fileDrop].join('\n');

  assert.doesNotMatch(sources, /__TAURI_INTERNALS__|__TAURI__/);
  assert.match(titlebar, /getCurrentWindow/);
  assert.match(folderPicker, /isTauri/);
  assert.match(fileDrop, /isTauri/);
});
