import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import SettingsPage from '../../src/pages/SettingsPage';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getMpvRuntime: vi.fn(),
  testMediaPaths: vi.fn(),
  patchConfig: vi.fn(),
  testMpv: vi.fn(),
  testTmdb: vi.fn(),
  openMpvConfigDir: vi.fn(),
}));

vi.mock('../../src/api/config', () => ({ configApi: api }));
vi.mock('../../src/api/tasks', () => ({ tasksApi: { list: vi.fn(async () => ({ tasks: [] })) } }));
vi.mock('../../src/api/openlist', () => ({
  openlistApi: {
    getRoutes: vi.fn(async () => ({ routes: [] })),
    discoverRoutes: vi.fn(async () => ({ items: [] })),
    saveConfig: vi.fn(),
    testConnection: vi.fn(),
    saveRoutes: vi.fn(),
  },
}));
vi.mock('../../src/stores/bangumi', () => ({
  useBangumiStore: (selector?: (state: unknown) => unknown) => {
    const state = {
      user: null,
      isLoggedIn: false,
      hasStoredCredential: false,
      credentialState: 'not_found',
      authStatus: 'unknown',
      connectivity: 'unknown',
      lastSuccessAt: '',
      loading: false,
      error: null,
      sessionStatus: 'signed_out',
      restoreSession: vi.fn(),
      verifySession: vi.fn(),
      setToken: vi.fn(),
      clearToken: vi.fn(),
    };
    return selector ? selector(state) : state;
  },
}));
vi.mock('../../src/stores/library', () => ({
  useLibraryStore: (selector: (state: unknown) => unknown) => selector({ loadLibrary: vi.fn() }),
}));
vi.mock('../../src/stores/ui', () => ({
  useUiStore: (selector?: (state: unknown) => unknown) => {
    const state = {
      appearanceMode: 'fluent',
      setAppearanceMode: vi.fn(),
      goPlayerTuning: vi.fn(),
    };
    return selector ? selector(state) : state;
  },
}));
vi.mock('../../src/components/settings/OpenListSettingsPanel', () => ({
  default: () => <div data-testid="openlist-settings-panel">既有 OpenList 连接设置</div>,
}));
vi.mock('../../src/components/settings/OpenListSourceRoutes', () => ({
  default: () => <div data-testid="openlist-source-routes">既有 OpenList 来源目录</div>,
}));

const config = {
  openlist_configured: true,
  openlist_server_url: 'https://openlist.example.test',
  openlist_remote_root: '/',
  openlist_mount_root: 'K:\\',
  openlist_cache_ttl_minutes: 1440,
  openlist_prefetch_limit: 12,
  pan115_root: 'K:\\115',
  baidu_root: 'K:\\Baidu',
  local_root: 'D:\\Media',
  directory_tree_dir: 'D:\\Trees',
  mirror_dir: 'D:\\Mirror',
};

describe('SettingsPage 信息架构', () => {
  beforeEach(() => {
    api.getConfig.mockResolvedValue(config);
    api.getMpvRuntime.mockResolvedValue({
      available: true,
      version: 'mpv 0.40.0',
      architecture: 'x86_64-pc-windows-msvc',
      target_triple: 'x86_64-pc-windows-msvc',
      manifest_valid: true,
      files_valid: true,
      configuration_available: true,
      scripts_available: true,
      distribution_status: 'development-only',
      message: '内置播放器已就绪',
    });
    Element.prototype.scrollIntoView = vi.fn();
    (globalThis as typeof globalThis & { IntersectionObserver: unknown }).IntersectionObserver = class {
      observe() {}
      disconnect() {}
    };
  });

  test('OpenList 设置导航定位到既有 OpenList 面板，媒体来源不再重复展示', async () => {
    render(<SettingsPage />);

    const openListNavigation = await screen.findByRole('button', { name: /OpenList 设置/ });
    const openListSection = document.getElementById('settings-panel-openlist');
    const sourceSection = document.getElementById('settings-panel-sources');

    expect(openListSection).toContainElement(screen.getByTestId('openlist-settings-panel'));
    expect(openListSection).toContainElement(screen.getByTestId('openlist-source-routes'));
    expect(sourceSection).not.toContainElement(screen.getByTestId('openlist-settings-panel'));
    expect(sourceSection).not.toContainElement(screen.getByTestId('openlist-source-routes'));

    fireEvent.click(openListNavigation);

    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
    expect(openListNavigation).toHaveAttribute('aria-current', 'location');
  });

  test('按 V4 数据流保留稳定设置分类，并将联网与重配入口放在对应位置', async () => {
    render(<SettingsPage />);

    const navigation = screen.getByRole('navigation', { name: '设置分类' });
    expect(await within(navigation).findByRole('button', { name: /媒体来源/ })).toBeVisible();
    expect(within(navigation).getByRole('button', { name: /账户与同步/ })).toBeVisible();
    expect(within(navigation).getByRole('button', { name: /元数据与图片/ })).toBeVisible();
    expect(within(navigation).getByRole('button', { name: /播放/ })).toBeVisible();
    expect(within(navigation).getByRole('button', { name: /外观/ })).toBeVisible();
    expect(within(navigation).queryByRole('button', { name: /应用与支持/ })).not.toBeInTheDocument();
    expect(document.getElementById('settings-panel-sources')).toHaveTextContent('115 挂载根路径');
    expect(document.getElementById('settings-panel-sources')).toHaveTextContent('镜像目录');
    expect(document.getElementById('settings-panel-sources')).toHaveTextContent('重新进入初始引导');
    expect(document.getElementById('settings-panel-scrape')).toHaveTextContent('网络代理');
  });

  test('将外观放在账户之后，便于首次进入设置时快速调整主题', async () => {
    render(<SettingsPage />);

    const navigation = screen.getByRole('navigation', { name: '设置分类' });
    const labels = within(navigation)
      .getAllByRole('button')
      .map((button) => button.textContent || '');

    expect(labels.findIndex((label) => label.includes('账户与同步'))).toBeLessThan(labels.findIndex((label) => label.includes('外观')));
    expect(labels.findIndex((label) => label.includes('外观'))).toBeLessThan(labels.findIndex((label) => label.includes('媒体来源')));
  });

  test('隐藏构建与赞助信息，并将播放器状态收敛为用户可理解的结果', async () => {
    render(<SettingsPage />);

    await screen.findByRole('button', { name: /OpenList 设置/ });

    expect(screen.queryByText('构建来源')).not.toBeInTheDocument();
    expect(screen.queryByText('支持与赞助')).not.toBeInTheDocument();
    expect(screen.queryByText('KumiPlayer 构建标识')).not.toBeInTheDocument();
    expect(screen.getByText('内置播放器已就绪')).toBeVisible();
    expect(screen.queryByText('x86_64-pc-windows-msvc')).not.toBeInTheDocument();
    expect(screen.queryByText(/清单：/)).not.toBeInTheDocument();
  });

  test('窄窗口定位时为粘性分类栏留出内容空间', async () => {
    const scrollTo = vi.fn();
    window.matchMedia = vi.fn().mockReturnValue({ matches: true });
    HTMLElement.prototype.scrollTo = scrollTo;
    render(<div className="app-main"><SettingsPage /></div>);

    fireEvent.click(await screen.findByRole('button', { name: /OpenList 设置/ }));

    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'auto' }));
  });
});
