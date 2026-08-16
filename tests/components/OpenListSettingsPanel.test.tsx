import { describe, expect, test, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import OpenListSettingsPanel, {
  type OpenListDraft,
  stateLabel,
} from '../../src/components/settings/OpenListSettingsPanel';
import type { PublicConfig } from '../../src/api/config';

function baseConfig(overrides: Partial<PublicConfig> = {}): PublicConfig {
  return {
    setup_completed: true,
    setup_version: 1,
    openlist_server_url: 'http://localhost:5244',
    openlist_remote_root: '/',
    openlist_mount_root: 'K:\\',
    openlist_configured: true,
    openlist_cache_ttl_minutes: 1440,
    openlist_prefetch_limit: 12,
    tmdb_bearer_token: '',
    proxy_url: '',
    ...overrides,
  } as unknown as PublicConfig;
}

function baseDraft(overrides: Partial<OpenListDraft> = {}): OpenListDraft {
  return {
    server_url: 'http://localhost:5244',
    remote_root: '/',
    mount_root: 'K:\\',
    username: '',
    password: '',
    cache_ttl: '1440',
    prefetch_limit: '12',
    ...overrides,
  };
}

function renderPanel(props: Partial<Parameters<typeof OpenListSettingsPanel>[0]> = {}) {
  const defaults = {
    config: baseConfig(),
    draft: baseDraft(),
    onChangeDraft: vi.fn(),
    onSaveConnection: vi.fn(async () => undefined),
    onTestConnection: vi.fn(async () => ({ ok: true, code: 'connected', phase: 'root', message: '连接成功' })),
    notice: '',
    noticeKind: 'info' as const,
    onNotice: vi.fn(),
    externalBusy: null,
  };
  const merged = { ...defaults, ...props };
  return {
    ...merged,
    ...render(<OpenListSettingsPanel {...merged} />),
  };
}

describe('OpenListSettingsPanel', () => {
  test('saved credential 显示“账号与密码已保存”，但不显示“连接正常”', () => {
    renderPanel();
    expect(screen.getByText('账号与密码已保存', { exact: false })).toBeTruthy();
    expect(screen.queryByText('OpenList 连接正常')).toBeNull();
  });

  test('未配置时暴露初始设置（用户名密码输入框可见）', () => {
    renderPanel({ config: baseConfig({ openlist_configured: false, openlist_server_url: '' }) });
    fireEvent.click(screen.getByText('管理连接'));
    // 未配置时用户名/密码输入框直接可见（credentialsOpen 初始 true）
    expect(screen.getByPlaceholderText('OpenList 用户名')).toBeTruthy();
    expect(screen.getByPlaceholderText('OpenList 密码')).toBeTruthy();
  });

  test('高级设置默认折叠', () => {
    renderPanel();
    fireEvent.click(screen.getByText('管理连接'));
    const advanced = screen.getByText('高级设置');
    expect(advanced).toBeTruthy();
    // details 默认闭合：缓存字段存在但不可见
    const cacheLabel = screen.getByText('目录浏览缓存（分钟）');
    expect(cacheLabel).toBeTruthy();
    expect((cacheLabel.closest('details') as HTMLDetailsElement | null)?.open).toBe(false);
  });

  test('已保存凭据时凭据编辑区默认折叠', () => {
    renderPanel();
    fireEvent.click(screen.getByText('管理连接'));
    expect(screen.getByText('更新账号或密码')).toBeTruthy();
    // 用户名/密码输入框不可见（需点击展开）
    expect(screen.queryByPlaceholderText('留空 = 使用已保存信息；填写 = 更新')).toBeNull();
  });

  test('remote-affecting 修改显示“验证并保存”', () => {
    renderPanel({ draft: baseDraft({ remote_root: '/new-root' }) });
    expect(screen.getByText('验证并保存')).toBeTruthy();
    expect(screen.queryByText('检查连接')).toBeNull();
  });

  test('local-only 修改显示“保存设置”', () => {
    renderPanel({ draft: baseDraft({ cache_ttl: '720' }) });
    expect(screen.getByText('保存设置')).toBeTruthy();
    expect(screen.queryByText('验证并保存')).toBeNull();
  });

  test('无修改时显示“检查连接”', () => {
    renderPanel();
    expect(screen.getByText('检查连接')).toBeTruthy();
  });

  test('状态文案：connected 与 credential_rejected 区分', () => {
    expect(stateLabel('connected')).toContain('连接正常');
    expect(stateLabel('credential_rejected')).toContain('拒绝了当前登录信息');
    expect(stateLabel('saved_unverified')).toContain('尚未检查当前连接');
    expect(stateLabel('network_unavailable')).toContain('不会被删除');
  });

  test('点击“管理连接”展开编辑区，展示 WebDAV 辅助信息', () => {
    renderPanel();
    fireEvent.click(screen.getByText('管理连接'));
    expect(screen.getByText('OpenList 地址', { exact: false })).toBeTruthy();
    expect(screen.getByText('WebDAV：', { exact: false })).toBeTruthy();
  });
});

describe('REWORK：状态与安全语义', () => {
  test('saved config 初始显示 saved_unverified（登录信息已保存，尚未检查当前连接）', async () => {
    renderPanel();
    expect(await screen.findByText(/尚未检查当前连接/)).toBeTruthy();
    expect(screen.queryByText('尚未配置')).toBeNull();
  });

  test('unconfigured 初始显示尚未配置', () => {
    renderPanel({ config: baseConfig({ openlist_configured: false, openlist_server_url: '' }) });
    expect(screen.getByText('尚未配置')).toBeTruthy();
  });

  test('credential_rejected → 显示凭据被拒，不是 network_unavailable', async () => {
    const { onTestConnection } = renderPanel({
      onTestConnection: vi.fn(async () => ({ ok: false, code: 'credential_rejected', phase: 'credential', message: '拒绝' })),
    });
    fireEvent.click(screen.getByText('检查连接'));
    expect(await screen.findByText(/拒绝了当前登录信息/)).toBeTruthy();
    expect(screen.queryByText(/暂时无法访问 OpenList 服务/)).toBeNull();
    expect(onTestConnection).toHaveBeenCalledTimes(1);
  });

  test('root_permission_denied → 对应状态', async () => {
    renderPanel({
      onTestConnection: vi.fn(async () => ({ ok: false, code: 'root_permission_denied', phase: 'root', message: '权限' })),
    });
    fireEvent.click(screen.getByText('检查连接'));
    expect(await screen.findByText(/没有读取权限/)).toBeTruthy();
  });

  test('rate_limited → 对应状态', async () => {
    renderPanel({
      onTestConnection: vi.fn(async () => ({ ok: false, code: 'rate_limited', phase: 'credential', message: '频繁' })),
    });
    fireEvent.click(screen.getByText('检查连接'));
    expect(await screen.findByText(/请求过于频繁/)).toBeTruthy();
  });

  test('非回环 HTTP 未勾选 → Test Connection 不得以 allow_insecure_http=true 发出', () => {
    const { onTestConnection } = renderPanel({
      config: baseConfig({ openlist_server_url: 'http://192.168.1.10:5244' }),
      draft: baseDraft({ server_url: 'http://192.168.1.10:5244' }),
    });
    fireEvent.click(screen.getByText('管理连接'));
    // 未勾选 → payload allow_insecure_http 必须为 false
    fireEvent.click(screen.getByText('检查连接'));
    expect(onTestConnection).toHaveBeenCalledWith(
      expect.objectContaining({ allow_insecure_http: false })
    );
  });

  test('勾选风险确认 → Test Connection 才允许 allow_insecure_http=true', () => {
    const { onTestConnection } = renderPanel({
      config: baseConfig({ openlist_server_url: 'http://192.168.1.10:5244' }),
      draft: baseDraft({ server_url: 'http://192.168.1.10:5244' }),
    });
    fireEvent.click(screen.getByText('管理连接'));
    fireEvent.click(screen.getByRole('checkbox', { name: /明文传输/ }));
    fireEvent.click(screen.getByText('检查连接'));
    expect(onTestConnection).toHaveBeenCalledWith(
      expect.objectContaining({ allow_insecure_http: true })
    );
  });

  test('快速双击检查连接 → 只有 1 个 in-flight action', async () => {
    let release!: (result: { ok: boolean; code: string; phase: string; message: string }) => void;
    const pending = new Promise<{ ok: boolean; code: string; phase: string; message: string }>((resolve) => { release = resolve; });
    const { onTestConnection } = renderPanel({ onTestConnection: vi.fn(() => pending) });
    fireEvent.click(screen.getByText('检查连接'));
    // 操作锁生效：第二次点击被抑制（按钮显示处理中且 actionLock 拒绝并发）
    fireEvent.click(screen.getByText('处理中…'));
    fireEvent.click(screen.getByText('处理中…'));
    release({ ok: true, code: 'connected', phase: 'root', message: 'ok' });
    await pending;
    expect(onTestConnection).toHaveBeenCalledTimes(1);
  });

  test('Save reject → busy 恢复 + 可见错误 + 无未处理 rejection', async () => {
    const { onSaveConnection, onNotice } = renderPanel({
      draft: baseDraft({ remote_root: '/new-root' }),
      onSaveConnection: vi.fn(async () => { throw new Error('保存失败：后端拒绝'); }),
    });
    fireEvent.click(screen.getByText('验证并保存'));
    // await 微任务让 promise 链完成
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onNotice).toHaveBeenCalledWith('保存失败：后端拒绝', 'error');
    // busy 已恢复：按钮重新可用（显示「验证并保存」而非「处理中…」）
    expect(screen.getByText('验证并保存')).toBeTruthy();
  });

  test('remote-affecting 保存成功 → connected；local-only 保存不宣称连接', async () => {
    renderPanel({ draft: baseDraft({ remote_root: '/new-root' }) });
    fireEvent.click(screen.getByText('验证并保存'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(await screen.findByText(/连接正常/)).toBeTruthy();
  });

  test('local-only 保存成功 → 保持 saved_unverified，不宣称 connected', async () => {
    renderPanel({ draft: baseDraft({ cache_ttl: '720' }) });
    fireEvent.click(screen.getByText('保存设置'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('OpenList 连接正常')).toBeNull();
    expect(await screen.findByText(/尚未检查当前连接/)).toBeTruthy();
  });
});

describe('REWORK：Test Connection payload 契约', () => {
  test('testConnection 只发送 TestConnectionRequest 接受的字段（不含 mount_root/cache_ttl）', () => {
    const { onTestConnection } = renderPanel({
      config: baseConfig(),
      draft: baseDraft({ server_url: 'http://192.168.1.10:5244', mount_root: 'M:\media', cache_ttl: '720' }),
    });
    // server_url 是 remote-affecting → 按钮为「验证并保存」；先展开并清空修改：
    // 用无 dirty 的 draft 重新渲染
    const panel2 = renderPanel({ onTestConnection });
    fireEvent.click(panel2.getByText('检查连接'));
    const payload = (onTestConnection as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload).toHaveProperty('server_url');
    expect(payload).toHaveProperty('remote_root');
    expect(payload).toHaveProperty('username');
    expect(payload).toHaveProperty('password');
    expect(payload).toHaveProperty('allow_insecure_http');
    // extra="forbid"：不得携带保存配置专用字段
    expect(payload).not.toHaveProperty('mount_root');
    expect(payload).not.toHaveProperty('cache_ttl_minutes');
    expect(payload).not.toHaveProperty('prefetch_limit');
  });
});
