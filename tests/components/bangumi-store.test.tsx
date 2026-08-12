import { beforeEach, expect, test, vi } from 'vitest';
import { bangumiApi } from '../../src/api/bangumi';
import { useBangumiStore } from '../../src/stores/bangumi';

const connectedSession = {
  credential_saved: true,
  status: 'connected' as const,
  user: {
    id: 1,
    username: 'tester',
    nickname: 'Tester',
    avatar: '',
    sign: '',
  },
  message: '',
};

beforeEach(() => {
  useBangumiStore.setState({
    user: null,
    isLoggedIn: false,
    loading: false,
    error: null,
    sessionStatus: 'checking',
    hasStoredCredential: false,
    firstConnectionRevision: 0,
  });
});

test('Bangumi 本次会话首次连通只发布一次刷新信号', async () => {
  vi.spyOn(bangumiApi, 'getSession').mockResolvedValue(connectedSession);

  await useBangumiStore.getState().restoreSession(1);
  expect(useBangumiStore.getState().firstConnectionRevision).toBe(1);

  useBangumiStore.setState({ sessionStatus: 'saved_offline', isLoggedIn: false });
  await useBangumiStore.getState().restoreSession(1);
  expect(useBangumiStore.getState().firstConnectionRevision).toBe(1);
});
