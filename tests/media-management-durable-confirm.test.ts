import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// 模块3 C2+C3 Review Fix B：V3 恢复链 durable confirm 分流 + queued 状态适配 契约测试。
// 静态契约模式（与 tests/*.test.ts 现有模式一致）：直接断言源码中的分流逻辑，
// 证明 confirmed preview 恢复场景同样走幂等 confirm → durable job 挂接，
// 不再掉回 legacy mirrorApi.generate() 造成双轨。

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const importsTs = readFileSync(new URL('../src/api/imports.ts', import.meta.url), 'utf8');

test('importsApi.confirm 响应类型包含 durable 分流字段', () => {
  assert.match(importsTs, /execution_mode\?: 'durable'/);
  assert.match(importsTs, /job_id\?: string/);
});

test('自动流水线：confirm 调用不再限定 draft（confirmed preview 恢复同样走幂等 confirm）', () => {
  // Blocker 3：旧目录树 preview 已 confirmed 的恢复场景（刷新页面）
  // 也必须调用 confirm，由后端幂等/ensure 语义返回 durable job_id。
  assert.match(page, /const confirmed = await importsApi\.confirm\(source, activeEntry\.planId\);/);
  assert.doesNotMatch(page, /previewStatus === 'draft'/);
  assert.doesNotMatch(page, /let durableTaskId = ''/);
});

test('自动流水线：V3 durable confirm 直接挂接后端任务，不再调用 mirrorApi.generate', () => {
  // durable + job_id → tasksApi.get(job_id)（不生成镜像）
  assert.match(page, /if \(confirmed\.execution_mode === 'durable' && confirmed\.job_id\)/);
  assert.match(page, /setTask\(await tasksApi\.get\(confirmed\.job_id\)\)/);
  // legacy 分支仍保留 mirrorApi.generate（旧 JSON 计划行为不变）
  assert.match(page, /const created = await mirrorApi\.generate\(source, activeEntry\.planId\);/);
});

test('durable 分支内不得出现 mirrorApi.generate 调用', () => {
  // 自动流水线：durable 判断块到 legacy else 之间，durable 路径只用
  // tasksApi.get(confirmed.job_id)，generate 只属于 legacy 分支。
  const start = page.indexOf("if (confirmed.execution_mode === 'durable' && confirmed.job_id) {");
  assert.ok(start >= 0, 'durable 分流块不存在');
  const elseAt = page.indexOf('} else {', start);
  assert.ok(elseAt > start, 'legacy else 分支不存在');
  const durablePath = page.slice(start, elseAt);
  assert.match(durablePath, /setTask\(await tasksApi\.get\(confirmed\.job_id\)\)/);
  assert.doesNotMatch(durablePath, /mirrorApi\.generate/);
});

test('confirmPlan：draft 与 confirmed 都走幂等 confirm 门面并挂接 durable job', () => {
  // 手动确认按钮同样走幂等确认门面：confirmCurrentImport() 内部完成
  // durable/legacy 分流，不再跳过 confirm 直接进工作台。
  const start = page.indexOf('const confirmPlan = async () =>');
  assert.ok(start >= 0, 'confirmPlan 不存在');
  const end = page.indexOf('const saveItem = async () =>', start);
  assert.ok(end > start, 'confirmPlan 区域边界未找到');
  const block = page.slice(start, end);
  // 幂等确认门面调用（draft 与 confirmed 均允许通过，不做 draft 专属判断）
  assert.match(block, /await confirmCurrentImport\(\);/);
  assert.match(block, /preview\.status !== 'draft' && preview\.status !== 'confirmed'/);
  assert.doesNotMatch(block, /preview\.status === 'draft'/);
  assert.doesNotMatch(block, /previewStatus === 'draft'/);
  // 手动路径 legacy 分支不自动生成镜像（进工作台由用户启动），保持现状
  assert.doesNotMatch(block, /mirrorApi\.generate/);
});

test('恢复链分流：confirmed preview + durable → tasksApi.get(job_id)，不出现 legacy 生成', () => {
  // 静态契约：auto-pipeline 的 confirm 调用为无条件语句（不再被 draft 状态
  // 分支包裹），恢复场景（previewStatus === 'confirmed'）同样命中 durable 挂接。
  const confirmAt = page.indexOf('const confirmed = await importsApi.confirm(source, activeEntry.planId);');
  assert.ok(confirmAt >= 0, 'auto-pipeline confirm 调用不存在');
  const before = page.slice(Math.max(0, confirmAt - 300), confirmAt);
  // confirm 调用之前的 300 字符内不允许出现 draft 条件包裹
  assert.doesNotMatch(before, /if \(previewStatus === 'draft'\)/);
  assert.doesNotMatch(before, /if \(preview\.status === 'draft'\)/);
  // durable 挂接与 legacy 生成二者必居其一，且 durable 分支优先挂接任务
  const after = page.slice(confirmAt);
  assert.match(after, /if \(confirmed\.execution_mode === 'durable' && confirmed\.job_id\)/);
  assert.match(after, /setTask\(await tasksApi\.get\(confirmed\.job_id\)\)/);
});

test('confirmed preview + 无 durable 标识 → legacy mirrorApi.generate 保留', () => {
  // 旧 JSON 计划：confirm 响应无 durable 字段时保持 legacy 生成路径（不丢失）
  const start = page.indexOf('const confirmed = await importsApi.confirm(source, activeEntry.planId);');
  assert.ok(start >= 0);
  const legacyGenerate = page.indexOf('const created = await mirrorApi.generate(source, activeEntry.planId);', start);
  assert.ok(legacyGenerate > start, 'legacy mirrorApi.generate 分支缺失');
  const between = page.slice(start, legacyGenerate);
  // legacy 分支路径由 else 承接（durable 判断后的兜底）
  assert.match(between, /} else \{/);
  // generate 之后立即挂接 legacy 任务
  const afterGenerate = page.slice(legacyGenerate, legacyGenerate + 200);
  assert.match(afterGenerate, /setTask\(await tasksApi\.get\(created\.task_id\)\)/);
});

// ============================================================
// 模块3 最终补完：V3 幂等恢复与旧链隔离（durable pipeline 不得掉回 legacy）
// ============================================================

test('isDurablePipelineTask 助手覆盖 mirror_revision 与 scrape_revision', () => {
  const helperAt = page.indexOf('function isDurablePipelineTask(');
  assert.ok(helperAt >= 0, 'isDurablePipelineTask 助手不存在');
  const helperEnd = page.indexOf('\n}', helperAt);
  assert.ok(helperEnd > helperAt);
  const helper = page.slice(helperAt, helperEnd);
  assert.match(helper, /mirror_revision/);
  assert.match(helper, /scrape_revision/);
});

test('handleWorkbenchStart：durable pipeline task 直接禁止 legacy fallback', () => {
  const start = page.indexOf('const handleWorkbenchStart = () => {');
  assert.ok(start >= 0, 'handleWorkbenchStart 不存在');
  const end = page.indexOf('const beginNewImport = () => {', start);
  assert.ok(end > start);
  const block = page.slice(start, end);
  // durable 守卫必须在任何 startTask 调用之前 return
  const guardAt = block.indexOf('isDurablePipelineTask(task)');
  assert.ok(guardAt >= 0, 'handleWorkbenchStart 缺少 durable 守卫');
  const firstStartTask = block.indexOf('void startTask(');
  assert.ok(firstStartTask > guardAt, 'durable 守卫必须位于 startTask 调用之前');
  const guardLine = block.slice(guardAt, block.indexOf('\n', guardAt));
  assert.match(guardLine, /return;/);
  // legacy 分流逻辑仍然保留（非 durable 路径行为不变）
  assert.match(block, /startTask\('scrape'\)/);
  assert.match(block, /startTask\('mirror'\)/);
});

test('mirror→scrape 自动推进 effect 明确排除 durable mirror_revision', () => {
  // 后端 durable mirror 完成后自己 enqueue durable scrape，前端不替它启动旧任务
  const at = page.indexOf("if (autoAdvanceScrapeRef.current === task.task_id) return;");
  assert.ok(at >= 0, 'auto-advance effect 不存在');
  const before = page.slice(Math.max(0, at - 250), at);
  assert.match(before, /isDurablePipelineTask\(task\)/);
  const durableGuardLine = before.split('\n').filter((line) => line.includes('isDurablePipelineTask'))[0];
  assert.match(durableGuardLine, /return;/);
});

test('durable pipeline task：工作台主按钮 disabled，不允许启动 legacy 任务', () => {
  const workbenchAt = page.indexOf("{step === 'workbench'");
  assert.ok(workbenchAt >= 0, 'workbench 区块不存在');
  const workbenchBlock = page.slice(workbenchAt);
  const disabledAt = workbenchBlock.indexOf('disabled={isScrapeTask(task) && taskKind');
  assert.ok(disabledAt >= 0, '工作台 disabled 表达式不存在');
  const disabledExpr = workbenchBlock.slice(disabledAt, workbenchBlock.indexOf('}', disabledAt));
  // 两种 mode 分支都必须包含 durable pipeline 守卫
  const durableGuardCount = (disabledExpr.match(/isDurablePipelineTask\(task\)/g) || []).length;
  assert.ok(durableGuardCount >= 2, `disabled 表达式两个 mode 分支都必须禁用 durable pipeline，实际 ${durableGuardCount} 处守卫`);
});
