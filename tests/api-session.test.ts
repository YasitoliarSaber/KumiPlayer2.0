import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  apiSessionHeaders,
  getApiSessionToken,
  setApiSessionToken,
  withApiSessionToken,
} from '../src/api/sessionToken.ts';


test('桌面会话令牌统一进入请求头', () => {
  setApiSessionToken('session-token');

  assert.equal(getApiSessionToken(), 'session-token');
  assert.deepEqual(apiSessionHeaders(), { 'X-KumiPlayer-Token': 'session-token' });
});


test('资源和 WebSocket 地址安全追加会话查询参数', () => {
  setApiSessionToken('a token/with symbols');

  assert.equal(
    withApiSessionToken('http://127.0.0.1:37821/api/assets?path=cover.jpg'),
    'http://127.0.0.1:37821/api/assets?path=cover.jpg&api_token=a%20token%2Fwith%20symbols',
  );
});


test('无桌面令牌的显式开发模式保持原地址', () => {
  setApiSessionToken('');

  assert.deepEqual(apiSessionHeaders(), {});
  assert.equal(withApiSessionToken('/api/assets?path=cover.jpg'), '/api/assets?path=cover.jpg');
});


test('远程图片只允许受信任 CDN 通过本地鉴权代理', () => {
  const source = readFileSync(new URL('../src/api/assets.ts', import.meta.url), 'utf8');

  assert.match(source, /image\.tmdb\.org/);
  assert.match(source, /s4\\\.anilist\\\.co/);
  assert.match(source, /API_BASE \+ '\/api\/assets\/remote\?url='/);
  assert.match(source, /return ''/);
});
