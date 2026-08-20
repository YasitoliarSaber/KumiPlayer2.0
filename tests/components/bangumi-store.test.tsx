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


const cachedUser = {
  id: 1,
  username: 'tester',
  nickname: 'Tester',
  avatar: '',
  sign: '',
};

function sessionWith(overrides: Partial<typeof connectedSession>): typeof connectedSession {
  return { ...connectedSession, user: cachedUser, ...overrides };
}

test('401 + cached user：身份保留、不视为已登录、sessionStatus=credential_invalid', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(
    sessionWith({ auth_status: 'reauth_required', connectivity: 'online' }),
  );
  await useBangumiStore.getState().restoreSession();
  const state = useBangumiStore.getState();
  expect(state.user?.username).toBe('tester'); // 身份保留
  expect(state.isLoggedIn).toBe(false);        // 不显示“已连接”
  expect(state.authStatus).toBe('reauth_required');
  expect(state.sessionStatus).toBe('credential_invalid');
});

test('offline + cached user：账户卡保留、不视为已登录', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(
    sessionWith({ auth_status: 'valid', connectivity: 'offline' }),
  );
  await useBangumiStore.getState().restoreSession();
  const state = useBangumiStore.getState();
  expect(state.user?.username).toBe('tester');
  expect(state.isLoggedIn).toBe(false); // valid 但 offline ≠ 已连接
  expect(state.connectivity).toBe('offline');
  expect(state.hasStoredCredential).toBe(true);
});

test('rate_limited + cached user：身份保留、不要求重新登录', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(
    sessionWith({ auth_status: 'valid', connectivity: 'rate_limited' }),
  );
  await useBangumiStore.getState().restoreSession();
  const state = useBangumiStore.getState();
  expect(state.user?.username).toBe('tester');
  expect(state.isLoggedIn).toBe(false);
  expect(state.connectivity).toBe('rate_limited');
  expect(state.sessionStatus).toBe('saved_offline'); // 非退出
});

test('credential unavailable + cached user：凭据不可读 ≠ signed_out', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(
    sessionWith({ credential_state: 'unavailable', credential_saved: true, auth_status: 'valid', connectivity: 'unknown' }),
  );
  await useBangumiStore.getState().restoreSession();
  const state = useBangumiStore.getState();
  expect(state.user?.username).toBe('tester'); // 身份保留
  expect(state.credentialState).toBe('unavailable');
  expect(state.sessionStatus).not.toBe('signed_out');
  expect(state.sessionStatus).toBe('saved_offline');
});

test('valid + online + user 才是已登录', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(
    sessionWith({ auth_status: 'valid', connectivity: 'online' }),
  );
  await useBangumiStore.getState().restoreSession();
  expect(useBangumiStore.getState().isLoggedIn).toBe(true);
  expect(useBangumiStore.getState().sessionStatus).toBe('connected');
});
