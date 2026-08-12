import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// 模块3 C2：V3 人工确认 split-brain 修复 —— 前端 durable confirm 分流契约测试。
// 静态契约模式（与 tests/*.test.ts 现有模式一致）：直接断言源码中的分流逻辑，
// 证明 durable confirm response → 不调用 mirrorApi.generate()。

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const importsTs = readFileSync(new URL('../src/api/imports.ts', import.meta.url), 'utf8');

test('importsApi.confirm 响应类型包含 durable 分流字段', () => {
  assert.match(importsTs, /execution_mode\?: 'durable'/);
  assert.match(importsTs, /job_id\?: string/);
});

test('自动流水线：V3 durable confirm 直接挂接后端任务，不再调用 mirrorApi.generate', () => {
  // durable + job_id → tasksApi.get(job_id)（不生成镜像）
  assert.match(page, /confirmed\.execution_mode === 'durable' && confirmed\.job_id/);
  assert.match(page, /setTask\(await tasksApi\.get\(durableTaskId\)\)/);
  assert.match(page, /setTask\(await tasksApi\.get\(confirmed\.job_id\)\)/);
  // legacy 分支仍保留 mirrorApi.generate（旧 JSON 计划行为不变）
  assert.match(page, /const created = await mirrorApi\.generate\(source, planId\);/);
});

test('durable 分支内不得出现 mirrorApi.generate 调用', () => {
  // 自动流水线：durableTaskId 设置块到 mirrorApi.generate 之间，
  // durable 路径只用 tasksApi.get(durableTaskId)，generate 只属于 legacy 分支
  const start = page.indexOf('let durableTaskId = \'\';');
  const generateAt = page.indexOf('const created = await mirrorApi.generate(source, planId);');
  const durablePath = page.slice(start, generateAt);
  assert.match(durablePath, /if \(durableTaskId\) \{/);
  assert.match(durablePath, /setTask\(await tasksApi\.get\(durableTaskId\)\)/);
  // durable 设置块内不允许出现 mirrorApi.generate
  const durableGuard = page.slice(
    page.indexOf("if (confirmed.execution_mode === 'durable' && confirmed.job_id) {"),
    page.indexOf('} else {', page.indexOf('durableTaskId = confirmed.job_id;')),
  );
  assert.doesNotMatch(durableGuard, /mirrorApi\.generate\(source, planId\)/);
});
