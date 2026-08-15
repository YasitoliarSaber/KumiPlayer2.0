import { beforeEach, expect, test, vi } from 'vitest';
import { bangumiApi } from '../../src/api/bangumi';
import { useBangumiStore } from '../../src/stores/bangumi';

const connectedSession = {
  credential_state: 'found' as const,
  credential_saved: true,
  auth_status: 'valid' as const,
  connectivity: 'online' as const,
  status: 'connected',
  user: {
    id: 1,
    username: 'tester',
    nickname: 'Tester',
    avatar: '',
    sign: '',
  },
  last_verified_at: '',
  last_success_at: '',
  last_failure_at: '',
  last_http_status: 200,
  last_error_code: '',
  last_error_message: '',
};

beforeEach(() => {
  useBangumiStore.setState({
    user: null,
    isLoggedIn: false,
    loading: false,
    error: null,
    sessionStatus: 'checking',
    hasStoredCredential: false,
    credentialState: 'unknown',
    authStatus: 'unknown',
    connectivity: 'unknown',
    lastVerifiedAt: '',
    lastSuccessAt: '',
    lastHttpStatus: null,
    lastErrorCode: '',
    lastErrorMessage: '',
    firstConnectionRevision: 0,
  });
});

test('Bangumi 本次会话首次连通只发布一次刷新信号', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(connectedSession);

  await useBangumiStore.getState().restoreSession();
  expect(useBangumiStore.getState().firstConnectionRevision).toBe(1);
  expect(useBangumiStore.getState().isLoggedIn).toBe(true);

  useBangumiStore.setState({ sessionStatus: 'saved_offline', isLoggedIn: false });
  await useBangumiStore.getState().restoreSession();
  expect(useBangumiStore.getState().firstConnectionRevision).toBe(1);
});

test('本地恢复 0 远程请求：restoreSession 只调用一次 getSession', async () => {
  const spy = vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(connectedSession);

  await useBangumiStore.getState().restoreSession();
  expect(spy).toHaveBeenCalledTimes(1);
  expect(useBangumiStore.getState().sessionStatus).toBe('connected');
  expect(useBangumiStore.getState().hasStoredCredential).toBe(true);
});
