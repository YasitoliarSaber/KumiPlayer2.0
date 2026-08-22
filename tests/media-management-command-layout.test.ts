import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const progress = readFileSync(new URL('../src/components/media/MediaFlowProgress.tsx', import.meta.url), 'utf8');
const workbench = readFileSync(new URL('../src/components/media/MediaTaskWorkbench.tsx', import.meta.url), 'utf8');

test('媒体导入使用三步进度导航并保留独立维护命令', () => {
  assert.match(progress, /label: '导入媒体'/);
  assert.match(progress, /label: '确认计划'/);
  assert.match(progress, /label: '创建媒体库并补充资料'/);
  assert.match(page, /<MediaFlowProgress/);
  assert.match(page, /className=\{`maintenance-nav-command \$\{step === 'maintenance' \? 'active' : ''\}`\}/);
  assert.match(styles, /\.media-workflow-progress\s*\{/);
  assert.match(styles, /\.maintenance-nav-command\.fui-Button\s*\{/);
});

test('顶部只有一个 command surface：header 透明，command-bar 承载全部涂层', () => {
  assert.match(styles, /\.media-flow-header\s*\{[^}]*background:\s*transparent[^}]*backdrop-filter:\s*none/s);
  assert.match(styles, /\.media-flow-command-bar\s*\{[^}]*grid-template-columns:\s*auto minmax\(0, 1fr\) auto[^}]*backdrop-filter:\s*blur\(14px\)/s);
  assert.doesNotMatch(styles, /\.media-flow-header\s*\{[^}]*backdrop-filter:\s*blur\(16px\)/);
  // 维护入口不再是第二张按钮卡：active 状态不再有 inset 底线
  assert.doesNotMatch(styles, /\.maintenance-nav-command\.active\.fui-Button\s*\{[^}]*box-shadow:\s*inset/s);
});

test('确认阶段默认可见媒体摘要，决策区与唯一主按钮同处一栏', () => {
  const confirmStage = page.slice(
    page.indexOf("{step === 'confirm' && preview && ("),
    page.indexOf("{step === 'workbench'"),
  );

  assert.match(confirmStage, /<MediaStageHeader[\s\S]*title="确认内容"/);
  assert.match(confirmStage, /className="media-plan-keyfacts"/);
  assert.match(confirmStage, /<MediaPlanSummary preview=\{preview\} \/>/);
  assert.match(confirmStage, /className="media-confirm-decision-layout"/);
  // 唯一主按钮：有可稍后处理项时显示“先确认并继续”，否则显示“确认并继续”
  assert.match(confirmStage, />\{reviewItems\.length \? '先确认并继续' : '确认并继续'\}<\/Button>/);
  assert.match(confirmStage, /logs=\{preview\.parse_logs\}/);
  // 旧“查看识别详情”默认折叠设计已删除，确认页也不混入历史任务日志
  assert.doesNotMatch(confirmStage, /<summary>查看识别详情<\/summary>/);
  assert.doesNotMatch(confirmStage, /查看识别详情|查看解析记录/);
  assert.doesNotMatch(confirmStage, /taskLogs/);
});

test('镜像与刮削由一个第三阶段工作台串联并自动推进', () => {
  // RWK-44 起 workbench 渲染拆为两个独立条件：TXT 进度展示优先，
  // 否则渲染 MediaTaskWorkbench（第三阶段工作台）串联镜像与刮削。
  assert.match(page, /step === 'workbench'/);
  assert.match(page, /\(preview \|\| isScrapeTask\(task\)\) && \(\s*$/m);
  assert.match(page, /<MediaTaskWorkbench/);
  assert.match(page, /mode=\{\s*isScrapeTask\(task\) && taskKind === 'scrape' \? 'scrape' : 'mirror'\s*\}/);
  assert.match(page, /isMirrorTaskReady\(task\)[\s\S]*startTask\('scrape'\)/);
  assert.match(workbench, /SUB_STAGES/);
  assert.match(workbench, /路径抽样验证/);
  assert.doesNotMatch(page, /function ExecutionStage\(/);
});

test('确认并继续在已确认状态下保持可点击，阻塞性问题与路径验证边界仍禁用', () => {
  const confirmStage = page.slice(
    page.indexOf("{step === 'confirm' && preview && ("),
    page.indexOf("{step === 'workbench'"),
  );
  const disabledExpression = confirmStage.match(/disabled=\{([^}]+)\}/)?.[1] || '';
  // 已确认计划不再因 preview.status !== 'draft' 被禁用
  assert.match(disabledExpression, /preview\.status !== 'draft' && preview\.status !== 'confirmed'/);
  // 阻塞性问题与路径验证失败仍然不可继续，不能被冗余继续绕过
  assert.match(disabledExpression, /blockingPreviewIssues\.length > 0/);
  assert.match(disabledExpression, /pathValidation\?\.ok === false/);
  // 可稍后处理项（reviewItems）不再阻塞：新行为是低置信项目先进入后台流程
  assert.doesNotMatch(disabledExpression, /reviewItems/);
});

test('确认并继续在 draft 与 confirmed 都走幂等确认门面，durable 挂接、legacy 不自动生成', () => {
  const confirmPlan = page.slice(
    page.indexOf('const confirmPlan = async () => {'),
    page.indexOf('const saveItem = async () => {'),
  );
  // 手动确认走幂等确认门面（薄封装，逻辑在 confirmCurrentImport 内）
  assert.match(confirmPlan, /await confirmCurrentImport\(\);/);
  assert.doesNotMatch(confirmPlan, /if \(preview\.status === 'draft'\)/);
  // 手动路径不直接创建镜像或刮削任务（门面内部分流，工作台展示进度）
  assert.doesNotMatch(confirmPlan, /mirrorApi\.generate|scrapeApi|startTask\(/);

  // 门面内部：confirm API 只出现一次，不再被 draft 状态分支包裹
  // （confirmed 恢复场景同样调用，由后端幂等/ensure 语义保证安全）
  const confirmCurrent = page.slice(
    page.indexOf('const confirmCurrentImport = async ()'),
    page.indexOf('const confirmPlan = async () => {'),
  );
  assert.equal((confirmCurrent.match(/importsApi\.confirm\(/g) || []).length, 1);
  assert.match(confirmCurrent, /await importsApi\.confirm\(source, activeEntry\.planId\);/);
  assert.doesNotMatch(confirmCurrent, /if \(preview\.status === 'draft'\)/);
  // durable 响应挂接已入队/恢复的镜像任务
  assert.match(confirmCurrent, /setTask\(await tasksApi\.get\(confirmed\.job_id\)\)/);
  // 确认后刷新预览并进入工作台
  assert.match(confirmCurrent, /importsApi\.getPreview\(source, activeEntry\.planId\)/);
  assert.match(confirmCurrent, /updateEntry\(activeEntry\.id, \{ preview: confirmedPreview, status: 'parsed' \}\)/);
  assert.match(confirmCurrent, /setStep\('workbench'\)/);
  // 刮削不在此处自动启动（由后端 durable 链或工作台手动启动）
  assert.doesNotMatch(confirmCurrent, /scrapeApi|startTask\(/);
});
