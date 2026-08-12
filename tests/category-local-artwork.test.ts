import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';


test('分类页海报网格只请求本地镜像图片', () => {
  const category = readFileSync(new URL('../src/pages/CategoryPage.tsx', import.meta.url), 'utf8');
  const grid = readFileSync(new URL('../src/components/library/VirtualizedPosterGrid.tsx', import.meta.url), 'utf8');
  const poster = readFileSync(new URL('../src/components/library/PosterCard.tsx', import.meta.url), 'utf8');

  assert.match(category, /<VirtualizedPosterGrid[\s\S]*?localArtworkOnly/);
  assert.match(grid, /localArtworkOnly/);
  assert.match(grid, /<PosterCard[\s\S]*?localArtworkOnly=\{localArtworkOnly\}/);
  assert.match(poster, /localArtworkOnly/);
  assert.match(poster, /isRemoteAssetPath/);
  assert.match(poster, /local_poster_path/);
});


test('搜索、收藏和详情场景仍保留受信任远程图片能力', () => {
  const search = readFileSync(new URL('../src/pages/SearchPage.tsx', import.meta.url), 'utf8');
  const favorites = readFileSync(new URL('../src/pages/FavoritesPage.tsx', import.meta.url), 'utf8');
  const assets = readFileSync(new URL('../src/api/assets.ts', import.meta.url), 'utf8');

  assert.doesNotMatch(search, /localArtworkOnly/);
  assert.doesNotMatch(favorites, /localArtworkOnly/);
  assert.match(assets, /image\.tmdb\.org/);
  assert.match(assets, /s4\\\.anilist\\\.co/);
  assert.match(assets, /\/api\/assets\/remote\?url=/);
});
