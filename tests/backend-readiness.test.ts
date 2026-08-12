import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { waitForDesktopBackend } from '../src/api/backendReadiness.ts';


test('桌面启动会等待受保护配置接口成功后再继续', async () => {
  let requests = 0;
  let waits = 0;

  await waitForDesktopBackend({
    attempts: 3,
    fetchHealth: async () => {
      requests += 1;
      if (requests < 3) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        json: async () => ({ setup_completed: true }),
      };
    },
    delay: async () => {
      waits += 1;
    },
  });

  assert.equal(requests, 3);
  assert.equal(waits, 2);
});


test('桌面后端持续未就绪时进入可恢复的启动失败', async () => {
  await assert.rejects(
    waitForDesktopBackend({
      attempts: 2,
      fetchHealth: async () => ({
        ok: false,
        json: async () => ({}),
      }),
      delay: async () => undefined,
    }),
    /后端在 30 秒内未能就绪/,
  );
});


test('桌面安全会话在 React 渲染前等待后端且网络错误使用中文提示', () => {
  const session = readFileSync(new URL('../src/api/desktopSession.ts', import.meta.url), 'utf8');
  const readiness = readFileSync(new URL('../src/api/backendReadiness.ts', import.meta.url), 'utf8');
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8');

  assert.match(session, /await waitForDesktopBackend\(\)/);
  assert.match(readiness, /http:\/\/127\.0\.0\.1:37821\/api\/config/);
  assert.match(readiness, /apiSessionHeaders\(\)/);
  assert.match(client, /error instanceof TypeError/);
  assert.match(client, /无法连接 KumiPlayer 后端/);
});
