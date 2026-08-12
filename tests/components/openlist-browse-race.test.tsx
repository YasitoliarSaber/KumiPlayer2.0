// OLIST-02-R1：OpenList 目录浏览竞争防护（RTL 状态测试）
// 场景：快速进入目录 A（慢响应）后立即点“上级”返回根（快响应），
// 慢的 A 响应不得覆盖较新的根状态；最终 UI 只显示根目录内容。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import MediaManagementPage from '../../src/pages/MediaManagementPage';
import { useMediaWorkflowStore } from '../../src/stores/mediaWorkflow';
import type { OpenListBrowseResult } from '../../src/api/openlist';

const rootPath = '/根目录';

const rootBrowse: OpenListBrowseResult = {
  path: rootPath,
  parent_path: null,
  remote_root: rootPath,
  entries: [
    { name: '目录A', is_dir: true, size: null, modified: null, remote_path: `${rootPath}/目录A` },
    { name: '目录B', is_dir: true, size: null, modified: null, remote_path: `${rootPath}/目录B` },
  ],
  total: 2,
  page: 1,
  per_page: 100,
  has_more: false,
  cache: { cached: false, status: 'none', refreshing: false, refresh_failed: false, fetched_at: null, expires_at: null },
};

const dirABrowse: OpenListBrowseResult = {
  path: `${rootPath}/目录A`,
  parent_path: rootPath,
  remote_root: rootPath,
  entries: [
    { name: '目录A2', is_dir: true, size: null, modified: null, remote_path: `${rootPath}/目录A/目录A2` },
  ],
  total: 1,
  page: 1,
  per_page: 100,
  has_more: false,
  cache: { cached: false, status: 'none', refreshing: false, refresh_failed: false, fetched_at: null, expires_at: null },
};

const dirA2Browse: OpenListBrowseResult = {
  path: `${rootPath}/目录A/目录A2`,
  parent_path: `${rootPath}/目录A`,
  remote_root: rootPath,
  entries: [
    { name: 'A2 的子文件.mkv', is_dir: false, size: 100, modified: 1, remote_path: `${rootPath}/目录A/目录A2/A2 的子文件.mkv` },
  ],
  total: 1,
  page: 1,
  per_page: 100,
  has_more: false,
  cache: { cached: false, status: 'none', refreshing: false, refresh_failed: false, fetched_at: null, expires_at: null },
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

vi.mock('../../src/api/config', () => ({
  configApi: {
    getConfig: vi.fn(),
  },
}));

vi.mock('../../src/api/openlist', () => ({
  openlistApi: {
    browse: vi.fn(),
    getRoutes: vi.fn(),
    prefetch: vi.fn(),
    discoverRoutes: vi.fn(),
    saveRoutes: vi.fn(),
    batchImport: vi.fn(),
    importRemote: vi.fn(),
    rescanPreset: vi.fn(),
    testConnection: vi.fn(),
    saveConfig: vi.fn(),
  },
}));

vi.mock('../../src/api/mediaPresets', () => ({
  mediaPresetsApi: {
    list: vi.fn(),
    create: vi.fn(),
    updateFromPath: vi.fn(),
    rescanLocal: vi.fn(),
    deletePreview: vi.fn(),
    deletePreset: vi.fn(),
    rebindRoot: vi.fn(),
    discard: vi.fn(),
  },
}));

vi.mock('../../src/api/tasks', () => ({
  tasksApi: {
    list: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
  },
}));

vi.mock('../../src/api/imports', () => ({
  importsApi: {
    getPreview: vi.fn(),
    confirmPlan: vi.fn(),
    executePlan: vi.fn(),
  },
}));

import { configApi } from '../../src/api/config';
import { openlistApi } from '../../src/api/openlist';
import { mediaPresetsApi } from '../../src/api/mediaPresets';
import { tasksApi } from '../../src/api/tasks';

const mockConfig = {
  setup_completed: true,
  setup_version: 1,
  mpv_path: '',
  server_port: 37821,
  mirror_dir: '',
  pan115_root: '',
  baidu_root: '',
  local_root: '',
  directory_tree_dir: '',
  openlist_server_url: 'https://ol.example.com',
  openlist_remote_root: rootPath,
  openlist_mount_root: 'K:\\',
  openlist_configured: true,
  openlist_cache_ttl_minutes: 1440,
  openlist_prefetch_limit: 12,
  openlist_routes: [],
  tmdb_bearer_token: '',
  tmdb_language: 'zh-CN',
  tmdb_certification_regions: 'CN',
  artwork_storage_mode: 'auto',
  anilist_enabled: true,
  anilist_rate_limit: 1,
  anilist_timeout: 10,
  deepseek_api_key: '',
  tmdb_rate_limit: 0.12,
  tmdb_max_retries: 2,
  tmdb_timeout: 10,
  bangumi_access_token: '',
  bangumi_user_agent: '',
  auto_play_next_episode: true,
  mpv_anime4k_mode: 'off',
  mpv_anime4k_quality: 'balanced',
  series_card_image_mode: 'poster',
  poster_size: 180,
  heartbeat_enabled: true,
  heartbeat_timeout: 30,
  auto_shutdown_on_heartbeat_timeout: false,
  proxy_url: '',
};

function renderPage() {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MediaManagementPage />
    </FluentProvider>,
  );
}

describe('OpenList 目录浏览竞争防护', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMediaWorkflowStore.getState().setSource('openlist');
    useMediaWorkflowStore.getState().setStep('import');
    vi.mocked(configApi.getConfig).mockResolvedValue(mockConfig as never);
    vi.mocked(openlistApi.getRoutes).mockResolvedValue({ routes: [] } as never);
    vi.mocked(openlistApi.prefetch).mockResolvedValue({ prefetched: 0, skipped: 0, busy: false } as never);
    vi.mocked(mediaPresetsApi.list).mockResolvedValue({ presets: [] } as never);
    vi.mocked(tasksApi.list).mockResolvedValue({ tasks: [] } as never);
  });

  test('快速进入慢目录后返回上级：慢响应不覆盖新状态，最终只显示根目录', async () => {
    const browseMock = vi.mocked(openlistApi.browse);
    // 根请求立即返回；目录A 慢（500ms）；目录B 快
    browseMock.mockImplementation(async (path: string) => {
      if (!path || path === rootPath) return rootBrowse as never;
      if (path === `${rootPath}/目录A`) return dirABrowse as never;
      if (path === `${rootPath}/目录A/目录A2`) {
        await sleep(500); // 慢响应
        return dirA2Browse as never;
      }
      return rootBrowse as never;
    });

    renderPage();

    // 进入导入模式（openlist 浏览器只在导入模式渲染）
    fireEvent.click(await screen.findByRole('button', { name: '添加媒体目录' }));
    const openlistChoice = screen.getByText('OpenList 连接');
    fireEvent.click(openlistChoice);

    // 根目录加载完成：出现两个目录按钮
    const dirAButton = await screen.findByRole('button', { name: /目录A/ });
    expect(dirAButton).toBeTruthy();
    expect(screen.getByRole('button', { name: /目录B/ })).toBeTruthy();

    // 进入目录A（快）→ 显示 A 内容（目录A2）
    fireEvent.click(dirAButton);
    await waitFor(() => expect(screen.getByRole('button', { name: /目录A2/ })).toBeTruthy());

    // 进入 A2（慢响应 500ms），随后立刻点“上级”返回 A（快响应）
    fireEvent.click(screen.getByRole('button', { name: /目录A2/ }));
    fireEvent.click(await screen.findByRole('button', { name: '上级' }));

    // 最终 UI 显示根目录内容（A/B 目录），而不是 A2 的子文件
    await waitFor(() => expect(screen.getByRole('button', { name: /目录B/ })).toBeTruthy());

    // 等慢响应返回后，A2 的子文件也不得出现（过期响应被丢弃）
    await sleep(700);
    expect(screen.queryByText('A2 的子文件.mkv')).toBeNull();
    expect(screen.getByRole('button', { name: /目录A/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /目录B/ })).toBeTruthy();
  });
});
