// RWK-40（P0-1）：durable_root 来源卡「继续确认」进入确认页的真实状态机测试。
// 事故链：TXT baseline 完成 → durable revisions 仍 draft → projector 投影
// lifecycle_status=needs_attention → 若 resumePreset 按 legacy lifecycle_status
// 判页面会进 workbench；必须按 durable confirmation identity（execution_authority
// == durable_root + confirmation_root_id + generation>0 + confirmation_ready）
// 进入确认页（step='confirm'）。
//
// 注意：durable confirm 页的 auto-pipeline effect（MediaManagementPage.tsx:330-350）
// 会在 step='confirm' 时自动调用 confirmCurrentImport()（durable → confirmRoot），
// 成功后 setStep('workbench')。因此「确认页被渲染」的证据 = getConfirmRootPreview
// 被调用（durable 分流）+ confirmRoot 被 auto-pipeline 触发（确认流程已启动）。
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import MediaManagementPage from '../../src/pages/MediaManagementPage';
import { useMediaWorkflowStore } from '../../src/stores/mediaWorkflow';
import type { MediaLibraryPreset } from '../../src/api/mediaPresets';

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
    revalidate: vi.fn(),
    get: vi.fn(),
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
    getConfirmRootPreview: vi.fn(),
    confirmPlan: vi.fn(),
    confirmRoot: vi.fn(),
    confirm: vi.fn(),
    executePlan: vi.fn(),
    resolveNeedsReview: vi.fn(),
  },
}));

vi.mock('../../src/api/mirror', () => ({
  mirrorApi: {
    generate: vi.fn(),
  },
}));

import { configApi } from '../../src/api/config';
import { mediaPresetsApi } from '../../src/api/mediaPresets';
import { openlistApi } from '../../src/api/openlist';
import { tasksApi } from '../../src/api/tasks';
import { importsApi } from '../../src/api/imports';
import { mirrorApi } from '../../src/api/mirror';

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
  openlist_remote_root: '/',
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

/** durable draft 场景：baseline 完成、draft revision 在确认集合、lifecycle 投影
 * 为 needs_attention（非 draft）——旧逻辑会送 workbench，必须进 confirm。 */
const durablePreset = {
  preset_id: 'preset-durable',
  name: '番剧目录',
  source: 'pan115' as const,
  source_root: 'D:/mount_old',
  import_family: 'anime' as const,
  import_scope: '' as const,
  update_mode: 'directory_tree' as const,
  lifecycle_status: 'needs_attention' as const,
  execution_authority: 'durable_root' as const,
  confirmation_root_id: 'root-1',
  confirmation_generation: 1,
  confirmation_ready: true,
  confirmation_blocked: false,
  confirmation_state: 'ready' as const,
  is_library_indexed: false,
  openlist_attention_count: 0,
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  current_snapshot_id: 's1',
  current_plan_id: 'plan-legacy',
  current_version_id: 'v1',
  version_count: 1,
  work_count: 0,
  video_count: 0,
  versions: [{
    version_id: 'v1',
    original_name: '115目录树.txt',
    archive_path: 'preset-durable/115目录树.txt',
    created_at: '2026-01-01T00:00:00',
    snapshot_id: 's1',
    plan_id: 'plan-legacy',
    diff_id: 'd1',
    summary: {},
    path_validation: { ok: true },
  }],
} as unknown as MediaLibraryPreset;

/** durable aggregate preview（确认页唯一真相来源） */
const durablePreview = {
  source: 'pan115',
  status: 'draft',
  issues: [],
  items: [{
    id: 'it1',
    work_title: '未识别作品',
    season_number: 1,
    episode_number: 1,
    group_type: 'season',
    resource_type: 'video',
    action: 'generate_strm',
    series_group: '',
    real_path: 'D:/mount_old/未识别作品/未识别作品.S01E01.mkv',
    source_path: '/动画/未识别作品/未识别作品.S01E01.mkv',
    needs_review: false,
    confidence: 'high',
  }],
  groups: [],
  parse_logs: [],
  summary: { total: 1, new_works: 1, work_count: 1 },
};

const fakeTask = {
  task_id: 'job-1',
  task_type: 'mirror_revision',
  job_type: 'mirror_revision',
  resource_key: 'mirror:rev-1',
  status: 'queued',
  payload: {},
  progress: 0,
};

function renderPage() {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MediaManagementPage />
    </FluentProvider>,
  );
}

describe('durable_root 来源卡「继续确认」进入确认页（真实状态机）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useMediaWorkflowStore.getState().setSource('pan115');
    useMediaWorkflowStore.getState().setStep('import');
    vi.mocked(configApi.getConfig).mockResolvedValue(mockConfig as never);
    vi.mocked(openlistApi.getRoutes).mockResolvedValue({ routes: [] } as never);
    vi.mocked(openlistApi.prefetch).mockResolvedValue({ prefetched: 0, skipped: 0, busy: false } as never);
    vi.mocked(tasksApi.list).mockResolvedValue({ tasks: [] } as never);
    vi.mocked(tasksApi.get).mockResolvedValue(fakeTask as never);
    vi.mocked(mediaPresetsApi.list).mockResolvedValue({ presets: [durablePreset] } as never);
    vi.mocked(importsApi.getConfirmRootPreview).mockResolvedValue(durablePreview as never);
    vi.mocked(importsApi.confirmRoot).mockResolvedValue({ job_ids: ['job-1'] } as never);
    vi.mocked(importsApi.confirm).mockResolvedValue({ execution_mode: 'legacy', job_id: '' } as never);
    vi.mocked(mirrorApi.generate).mockResolvedValue({ task_id: 'task-1' } as never);
  });

  test('durable draft 卡点击「继续确认」→ 读取 aggregate preview 并进入确认页', async () => {
    renderPage();

    // 来源卡出现「继续确认」（durable_root + confirmation_ready=true）
    const continueButton = await screen.findByRole('button', { name: '继续确认' });
    expect(continueButton).toBeTruthy();

    fireEvent.click(continueButton);

    // P0-1 核心：resumePreset 必须读取 durable aggregate preview（root_id,
    // generation），而不是 legacy preview——这是 durable 分流进 confirm 的直接证据。
    await waitFor(() => {
      expect(importsApi.getConfirmRootPreview).toHaveBeenCalledWith('pan115', 'root-1', 1);
    });

    // 确认页的 auto-pipeline（仅 step='confirm' 时触发）自动确认 durable root →
    // confirmRoot 被调用。这证明确实进入了确认页（step='confirm'），而不是
    // workbench（workbench 不会触发 confirmRoot）。
    await waitFor(() => {
      expect(importsApi.confirmRoot).toHaveBeenCalledWith('pan115', 'root-1', 1);
    });

    // legacy getPreview 不得被调用（durable 卡不读 legacy plan）
    expect(importsApi.getPreview).not.toHaveBeenCalled();
  });

  test('legacy draft 卡仍按 lifecycle draft 进确认页（不回归 legacy 行为）', async () => {
    // legacy preset：无 durable identity，lifecycle_status=draft
    const legacyPreset = {
      ...durablePreset,
      preset_id: 'preset-legacy',
      name: '旧版目录',
      lifecycle_status: 'draft' as const,
      execution_authority: '' as const,
      confirmation_root_id: undefined,
      confirmation_generation: undefined,
      confirmation_ready: undefined,
      confirmation_blocked: undefined,
      confirmation_state: undefined,
      update_mode: 'directory_tree' as const,
    } as unknown as MediaLibraryPreset;
    vi.mocked(mediaPresetsApi.list).mockResolvedValue({ presets: [legacyPreset] } as never);
    vi.mocked(importsApi.getPreview).mockResolvedValue(durablePreview as never);

    renderPage();

    const continueButton = await screen.findByRole('button', { name: /继续处理/ });
    expect(continueButton).toBeTruthy();

    fireEvent.click(continueButton);

    // legacy：走 getPreview(source, current_plan_id)——无 durable identity 的兜底
    await waitFor(() => {
      expect(importsApi.getPreview).toHaveBeenCalledWith('pan115', 'plan-legacy');
    });

    // legacy confirm 页的 auto-pipeline 调 importsApi.confirm（非 durable confirmRoot）——
    // 证明 legacy 也进入确认页且沿用 legacy 分流，不误走 durable。
    await waitFor(() => {
      expect(importsApi.confirm).toHaveBeenCalledWith('pan115', 'plan-legacy');
    });

    // legacy 绝不调 durable 专属端点
    expect(importsApi.getConfirmRootPreview).not.toHaveBeenCalled();
    expect(importsApi.confirmRoot).not.toHaveBeenCalled();
  });
});
