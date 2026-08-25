import assert from 'node:assert/strict';
import test from 'node:test';

import { preferredArtworkPath } from '../src/utils/artwork.ts';

test('已物化的本地图片优先于远程元数据地址', () => {
  const work = {
    poster_path: 'https://image.tmdb.org/t/p/w342/poster.jpg',
    fanart_path: 'https://image.tmdb.org/t/p/w1280/fanart.jpg',
    clearlogo_path: 'https://image.tmdb.org/t/p/w300/logo.png',
    local_poster_path: 'D:/mirror/work/poster.jpg',
    local_fanart_path: 'D:/mirror/work/fanart.jpg',
    local_clearlogo_path: 'D:/mirror/work/clearlogo.png',
  };

  assert.equal(preferredArtworkPath(work, 'poster'), 'D:/mirror/work/poster.jpg');
  assert.equal(preferredArtworkPath(work, 'fanart'), 'D:/mirror/work/fanart.jpg');
  assert.equal(preferredArtworkPath(work, 'clearlogo'), 'D:/mirror/work/clearlogo.png');
});

test('本地图片缺失时保留远程图片作为兜底', () => {
  assert.equal(
    preferredArtworkPath({ fanart_path: 'https://image.tmdb.org/t/p/w1280/fanart.jpg' }, 'fanart'),
    'https://image.tmdb.org/t/p/w1280/fanart.jpg',
  );
});
