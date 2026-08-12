import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const splash = readFileSync(new URL('../src/components/shell/StartupSplash.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');
const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const tauri = JSON.parse(readFileSync(new URL('../src-tauri/tauri.conf.json', import.meta.url), 'utf8')) as {
  app: { windows: Array<{ transparent?: boolean; windowEffects?: { effects?: string[] }; backgroundColor?: string }> };
};
const icon = readFileSync(new URL('../public/brand/kumiplayer-app-icon.svg', import.meta.url), 'utf8');

test('启动首帧与 React 兜底均不再包含旋转进度环', () => {
  assert.doesNotMatch(html, /kumi-boot-track/);
  assert.doesNotMatch(html, /kumi-boot-turn/);
  assert.doesNotMatch(html, /@keyframes[\s\S]*rotate\(/);
  assert.doesNotMatch(splash, /startup-splash-track/);
  assert.doesNotMatch(styles, /\.startup-splash-track/);
  assert.doesNotMatch(styles, /startup-track-turn/);
});

test('第二版：主题背景直接出现，不透明，无磨砂层与亚克力', () => {
  // 静态首帧背景直接为 boot-bg，非 transparent
  assert.match(html, /#kumi-boot-splash \{[\s\S]*?background:\s*var\(--kumi-boot-bg\)/);
  assert.doesNotMatch(html, /#kumi-boot-splash\s*\{[\s\S]*?background:\s*transparent/);
  // 不存在磨砂层 ::before 与主题淡入 ::after
  assert.doesNotMatch(html, /#kumi-boot-splash::before/);
  assert.doesNotMatch(html, /#kumi-boot-splash::after/);
  assert.doesNotMatch(html, /kumi-boot-frost-out|kumi-boot-bg-in/);
  // React 兜底同步：背景为 boot-bg，无 ::before/::after
  assert.match(styles, /\.startup-splash \{[\s\S]*?background:\s*var\(--kumi-boot-bg, var\(--app-bg\)\)/);
  assert.doesNotMatch(styles, /\.startup-splash::before/);
  assert.doesNotMatch(styles, /\.startup-splash::after/);
  assert.doesNotMatch(styles, /startup-frost-out|startup-bg-in/);
});

test('第二版：Tauri 窗口不透明，无 transparent 与 windowEffects，恢复 backgroundColor', () => {
  const win = tauri.app.windows[0];
  assert.notEqual(win.transparent, true);
  assert.equal(win.windowEffects, undefined);
  assert.equal(win.backgroundColor, '#eef3f9');
});

test('图标无外框托板：静态首帧与 React 兜底均不含 mark::before 托板', () => {
  assert.doesNotMatch(html, /\.kumi-boot-mark::before/);
  assert.doesNotMatch(styles, /\.startup-splash-mark::before/);
  assert.doesNotMatch(html, /\.kumi-boot-mark img[\s\S]*box-shadow/);
});

test('图标更大：启动首帧图标尺寸不小于 96px', () => {
  assert.match(html, /\.kumi-boot-mark img \{[\s\S]*width:\s*(9[6-9]|[1-9]\d{2,})px/);
  assert.match(styles, /\.startup-splash-mark img \{[\s\S]*width:\s*(9[6-9]|[1-9]\d{2,})px/);
});

test('三主题背景直接出现：各自 boot-bg 色值', () => {
  assert.match(html, /html\[data-theme='fluent'\] \{[\s\S]*?--kumi-boot-bg:\s*#eef3f9/);
  assert.match(html, /html\[data-theme='cinema'\] \{[\s\S]*?--kumi-boot-bg:\s*#080808/);
  assert.match(html, /html\[data-theme='mica'\] \{[\s\S]*?--kumi-boot-bg:\s*#ffffff/);
});

test('reduced-motion 下图标直接显示，无动画', () => {
  assert.match(html, /html\[data-motion='reduced'\] \.kumi-boot-mark \{[\s\S]*?opacity:\s*1;[\s\S]*?animation:\s*none;/);
  assert.match(html, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.kumi-boot-mark \{[\s\S]*?opacity:\s*1;[\s\S]*?animation:\s*none;/);
  assert.match(styles, /:root\[data-motion='reduced'\] \.startup-splash-mark \{[\s\S]*?opacity:\s*1;[\s\S]*?animation:\s*none;/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.startup-splash-mark \{[\s\S]*?opacity:\s*1;[\s\S]*?animation:\s*none;/);
});

test('configReady 同步移除静态首帧，无最短展示时间与动画等待', () => {
  assert.match(app, /useLayoutEffect\(\(\) => \{[\s\S]*?if \(!configReady\) return;[\s\S]*?kumi-boot-splash'\)\?\.remove\(\);/);
  assert.doesNotMatch(app, /setTimeout\([\s\S]{0,80}kumi-boot-splash/);
  assert.doesNotMatch(app, /animationend/);
});

test('静态首帧与 React 兜底共用同一图标与居中结构', () => {
  assert.match(html, /<div class="kumi-boot-mark">\s*<img src="\/brand\/kumiplayer-app-icon\.svg" alt="" \/>/);
  assert.match(splash, /<div className="startup-splash-mark" aria-hidden="true">\s*<img src="\/brand\/kumiplayer-app-icon\.svg" alt="" \/>/);
  assert.match(html, /#kumi-boot-splash \{[\s\S]*?place-items:\s*center/);
  assert.match(styles, /\.startup-splash \{[\s\S]*?place-items:\s*center/);
});

test('图标入场仅 opacity 与 transform 一次性落位', () => {
  assert.match(html, /@keyframes kumi-boot-enter \{\s*to \{ opacity: 1; transform: scale\(1\); \}\s*\}/);
  assert.match(styles, /@keyframes startup-mark-enter \{\s*to \{ opacity: 1; transform: scale\(1\); \}\s*\}/);
});

test('图标 SVG 为恢复的设计：黑壳 + 内白环 + 四角高光弧 + 曲线白 K，无蓝折角', () => {
  // 黑色 squircle 壳
  assert.match(icon, /#1a1a1a/i);
  // 白色内环
  assert.match(icon, /<rect[^>]*stroke="#ffffff"[^>]*fill="none"/i);
  // 四角高光弧
  assert.match(icon, /A180 180/);
  // 曲线白 K（含 Q 二次贝塞尔曲线）
  assert.match(icon, /stroke="#ffffff"[^>]*stroke-width="72"/i);
  assert.match(icon, /Q\d/);
  // 无蓝色折角
  assert.doesNotMatch(icon, /#3b82f6/i);
  // aria-label 保留
  assert.match(icon, /aria-label="KumiPlayer"/);
  // 无 linearGradient
  assert.doesNotMatch(icon, /linearGradient/);
});
