import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const detail = readFileSync(new URL('../src/pages/WorkDetailPage.tsx', import.meta.url), 'utf8');
const posterCard = readFileSync(new URL('../src/components/library/PosterCard.tsx', import.meta.url), 'utf8');
const home = readFileSync(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8');

test('详情首屏不重复挂载背景图，并优先消费本地 artwork', () => {
  assert.match(detail, /preferredArtworkPath\(work, 'fanart'\)/);
  assert.match(detail, /preferredArtworkPath\(work, 'clearlogo'\)/);
  assert.match(detail, /className="detail-hero-art"[\s\S]*loading="eager"/);
  assert.doesNotMatch(detail, /className="detail-hero-bg"/);
});

test('非首屏详情缩略图与所有浏览卡片不抢占首屏图片请求', () => {
  assert.match(detail, /loading=\{episodeIndex < 6 \? 'eager' : 'lazy'\}/);
  assert.match(detail, /person\.profile_path[\s\S]*loading="lazy"/);
  assert.match(posterCard, /selectedImagePath = preferredArtworkPath\(work, artworkKind\)/);
  assert.match(home, /preferredArtworkPath\(work, 'fanart'\)/);
});

test('标题图和首屏剧集图在加载完成后立即显现，背景图仍保持完整解码切换', () => {
  assert.match(detail, /className="detail-hero-logo"[\s\S]{0,220}revealOnLoad/);
  assert.match(detail, /loading=\{episodeIndex < 6 \? 'eager' : 'lazy'\}[\s\S]{0,180}revealOnLoad/);
  assert.doesNotMatch(detail, /className="detail-hero-art"[\s\S]{0,180}revealOnLoad/);
});
