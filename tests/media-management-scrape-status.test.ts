import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/MediaManagementPage.tsx', import.meta.url), 'utf8');

// 只抽取档案卡渲染段，避免被页面其他区域文案干扰。
const cardRender = page.slice(
  page.indexOf('const renderPresetCards'),
  page.indexOf('const confirmPlan'),
);

test('有 review 项时档案卡仍保留人工匹配入口与待处理数量', () => {
  // 人工匹配入口由 hasManualReview 门控，且仍走 openScrapeReview 弹窗链路
  assert.match(cardRender, /const hasManualReview = preset\.lifecycle_status === 'needs_attention' && \(preset\.review_count \|\| 0\) > 0;/);
  assert.match(cardRender, /hasManualReview && <Button[\s\S]*onClick=\{\(\) => void openScrapeReview\(preset\)\}>处理刮削匹配<\/Button>/);
  // 状态文案展示真实数量，不再用 || 0 兜底
  assert.match(cardRender, /hasManualReview \? `需处理 \$\{preset\.review_count\} 个刮削匹配`/);
});

test('review_count 为 0 时不再显示“需处理 0 个刮削匹配”或“处理刮削匹配”', () => {
  // 旧版“需处理 ${preset.review_count || 0} 个刮削匹配”模板必须消失
  assert.doesNotMatch(cardRender, /\$\{preset\.review_count \|\| 0\} 个刮削匹配/);
  assert.doesNotMatch(cardRender, /需处理 0 个刮削匹配/);
  // 人工匹配按钮不能再无条件跟随 needs_attention 出现
  assert.doesNotMatch(cardRender, /needs_attention' && <Button[\s\S]*>处理刮削匹配<\/Button>/);
});

test('刮削失败判定覆盖 status failed、result.error 与 result.failed > 0 三类信号', () => {
  // 失败判定同时覆盖任务级 status、结果级 error 与失败计数
  assert.match(cardRender, /scrapeTask\?\.status === 'failed'/);
  assert.match(cardRender, /Boolean\(String\(scrapeResult\.error \|\| ''\)\.trim\(\)\)/);
  assert.match(cardRender, /Number\(scrapeResult\.failed \|\| 0\) > 0/);
});

test('失败原因按 error、result.error、失败计数兜底文案的优先级提供', () => {
  // 原因优先取任务 error，其次取结果 error
  assert.match(cardRender, /scrapeTask\?\.error \|\| scrapeResult\.error/);
  // 仅 result.failed > 0 且无文字原因时使用稳定兜底文案
  assert.match(cardRender, /Number\(scrapeResult\.failed \|\| 0\) > 0 \? '部分刮削目标处理失败，请重新刮削' : ''/);
  assert.match(cardRender, /部分刮削目标处理失败，请重新刮削/);
});

test('失败场景显示失败语义并提供“重新刮削”入口复用 queuePresetScrape', () => {
  assert.match(cardRender, /上次刮削失败/);
  assert.match(cardRender, /scrapeFailReason/);
  // 重新刮削按钮只在任务不活跃、判定失败且没有人工匹配入口时出现
  assert.match(cardRender, /!scrapeActive && scrapeFailed && !hasManualReview && <Button/);
  assert.match(cardRender, /onClick=\{\(\) => void queuePresetScrape\(preset\)\}>重新刮削<\/Button>/);
  // 镜像态的“加入刮削队列/继续刮削”与失败态互斥，避免出现两个重复提交入口
  assert.match(cardRender, /!scrapeActive && !scrapeFailed && preset\.lifecycle_status === 'mirrored' && <Button/);
});

test('review 项与失败信号并存时人工匹配为主入口，不并列第二个主操作', () => {
  // 处理刮削匹配保留为主入口；重新刮削以 !hasManualReview 为门控，二者互斥不会并列渲染
  assert.match(cardRender, /hasManualReview && <Button[\s\S]*>处理刮削匹配<\/Button>/);
  assert.match(cardRender, /scrapeFailed && !hasManualReview && <Button[\s\S]*>重新刮削<\/Button>/);
  // 失败信息在状态文案中仍然可见
  assert.match(cardRender, /scrapeFailed \? `上次刮削失败/);
});

test('cancelled 与“已停止”兼容语义不被误判为刮削失败', () => {
  assert.match(cardRender, /const scrapeStopped = scrapeTask\?\.status === 'cancelled'/);
  assert.match(cardRender, /scrapeTask\?\.message === '已停止'/);
  // 失败判定必须排除停止语义
  assert.match(cardRender, /const scrapeFailed = !scrapeStopped && \(/);
  assert.match(cardRender, /上次刮削已停止/);
});
