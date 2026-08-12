import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('桌面 WebView 和 HTML 启动层使用同一非白色首帧背景', () => {
  const config = JSON.parse(readFileSync(new URL('../src-tauri/tauri.conf.json', import.meta.url), 'utf8'));
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

  assert.equal(config.app.windows[0].backgroundColor, '#eef3f9');
  assert.notEqual(config.app.windows[0].transparent, true);
  assert.equal(config.app.windows[0].windowEffects, undefined);
  assert.match(html, /--kumi-boot-bg:\s*#eef3f9/);
  assert.match(html, /#kumi-boot-splash\s*\{[^}]*background:\s*var\(--kumi-boot-bg\)/s);
});

test('主要媒体入口统一等待图片解码，避免半张图和白底逐行出现', () => {
  const poster = readFileSync(new URL('../src/components/library/PosterCard.tsx', import.meta.url), 'utf8');
  const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');
  const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
  const decodedImage = readFileSync(new URL('../src/components/ui/DecodedImage.tsx', import.meta.url), 'utf8');

  assert.match(decodedImage, /image\.decode\(\)/);
  assert.match(decodedImage, /data-image-state/);
  assert.match(poster, /<DecodedImage/);
  assert.match(home, /<DecodedImage/g);
  assert.match(detail, /<DecodedImage/g);
  assert.doesNotMatch(poster, /<img\s/);
  assert.doesNotMatch(home, /<img\s/);
  assert.doesNotMatch(detail, /<img\s/);
});
