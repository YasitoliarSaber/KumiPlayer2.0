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
    onTestConnection: vi.fn(async () => undefined),
    notice: '',
    noticeKind: 'info' as const,
    busy: null,
  };
  return render(<OpenListSettingsPanel {...defaults} {...props} />);
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
