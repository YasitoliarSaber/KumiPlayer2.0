/** P-006 新番追更入口：分类页扫描按钮、追更卡标签。 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import CategoryPage from '../../src/pages/CategoryPage';
import PosterCard from '../../src/components/library/PosterCard';

const api = vi.hoisted(() => ({
  trackingScanAll: vi.fn(),
  trackingCancelScan: vi.fn(),
  trackingWorks: vi.fn(),
  trackingSources: vi.fn(),
  trackingScanWork: vi.fn(),
  sourceLibraries: vi.fn(),
  workExecutionDetail: vi.fn(),
  openlistStatus: vi.fn(),
  drafts: vi.fn(),
  goManageView: vi.fn(),
}));
vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: api }));
vi.mock('../../src/api/library', () => ({ v4LibraryApi: { getWorkDetail: vi.fn().mockResolvedValue({}) } }));
vi.mock('../../src/stores/library', () => ({
  useLibraryStore: (selector: (state: unknown) => unknown) => selector({
    works: [
      { work_id: 'w1', title: '追更作品', year: 2026, media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: { work_id: 'w1', status: 'watching', note: '', favorite: false, updated_at: 'now' }, latest_episode_number: 5 },
      { work_id: 'w2', title: '普通作品', year: 2026, media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: null },
    ],
    history: [],
    loading: false,
    error: null,
  }),
}));
vi.mock('../../src/stores/ui', () => {
  const state = {
    activeCategory: 'seasonal',
    source: 'all',
    sort: 'recent',
    setSort: vi.fn(),
    posterSize: 'medium',
    consumeCategoryScrollRestore: vi.fn().mockReturnValue(0),
    goManageView: api.goManageView,
  };
  const useUiStore = (selector?: (value: typeof state) => unknown) => selector ? selector(state) : state;
  useUiStore.getState = () => state;
  return { useUiStore };
});
vi.mock('../../src/platform/folderPicker', () => ({ pickDirectoryTreeFile: vi.fn(), pickFolder: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear();
  localStorage.clear();
  api.trackingSources.mockResolvedValue({ sources: [] });
  api.workExecutionDetail.mockResolvedValue({ has_detail: false });
  api.trackingScanAll.mockResolvedValue({
    tasks: [
      { task_id: 'scan-1', root_id: 'r1', remote_root: '/Anime', status: 'running' },
    ],
  });
  api.trackingCancelScan.mockResolvedValue({ scan_id: 'scan-1', status: 'cancelling' });
});

afterEach(() => vi.useRealTimers());

test('新番分类显示扫描入口并调用 V4 tracking 命令', async () => {
  render(<CategoryPage />);
  expect(screen.getByRole('button', { name: '刷新新番' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '刷新新番' }));
  await waitFor(() => expect(api.trackingScanAll).toHaveBeenCalled());
  expect(await screen.findByText('正在更新')).toBeVisible();
});

test('新番扫描可取消', async () => {
  render(<CategoryPage />);
  fireEvent.click(screen.getByRole('button', { name: '刷新新番' }));
  const cancel = await screen.findByRole('button', { name: '取消更新' });
  fireEvent.click(cancel);
  await waitFor(() => expect(api.trackingCancelScan).toHaveBeenCalledWith('scan-1'));
});

test('刷新显示排队、待确认、需新TXT和失败真实状态，仅活动任务可取消', async () => {
  api.trackingScanAll.mockResolvedValue({ tasks: [
    { task_id: 'queued-1', root_id: 'r1', display_name: '本地新番', status: 'queued' },
    { task_id: '', root_id: 'r2', display_name: '识别待处理', status: 'needs_confirmation', draft_revision_id: 'draft-2' },
    { task_id: '', root_id: 'r3', display_name: 'TXT新番', status: 'needs_new_txt', reason: '请提供新导出的目录树 TXT' },
    { task_id: '', root_id: 'r4', display_name: '失败来源', status: 'failed', reason: '本次更新失败' },
  ] });
  render(<CategoryPage />);
  fireEvent.click(screen.getByRole('button', { name: '刷新新番' }));
  expect(await screen.findByText('等待更新')).toBeVisible();
  expect(screen.getByText('待人工确认')).toBeVisible();
  expect(screen.getByText('需要新 TXT')).toBeVisible();
  expect(screen.getByText('更新失败')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '取消更新' }));
  await waitFor(() => expect(api.trackingCancelScan).toHaveBeenCalledWith('queued-1'));
  expect(api.trackingCancelScan).toHaveBeenCalledTimes(1);
});

test('待确认来源进入现有媒体管理识别入口', async () => {
  api.trackingSources.mockResolvedValue({ sources: [
    { root_id: 'review-root', display_name: '待确认来源', status: 'needs_confirmation', draft_revision_id: 'draft-review' },
  ] });
  render(<CategoryPage />);
  fireEvent.click(await screen.findByRole('button', { name: '查看识别结果' }));
  expect(api.goManageView).toHaveBeenCalledWith('overview');
  expect(JSON.parse(sessionStorage.getItem('kumiplayer.media-v4.ongoing-source-action') || '{}')).toEqual({
    root_id: 'review-root', action: 'review', revision_id: 'draft-review',
  });
});

test('活动更新轮询来源状态，完成后降低频率，退出页停止，不自动发起扫描', async () => {
  vi.useFakeTimers();
  api.trackingSources.mockResolvedValueOnce({ sources: [{ root_id: 'r1', task_id: 'scan-1', display_name: '本地新番', status: 'queued' }] })
    .mockResolvedValue({ sources: [{ root_id: 'r1', task_id: '', display_name: '本地新番', status: 'completed' }] });
  const { unmount } = render(<CategoryPage />);
  await act(async () => {});
  expect(screen.getByText('等待更新')).toBeVisible();
  await act(async () => vi.advanceTimersByTimeAsync(1600));
  expect(screen.getByText('更新完成')).toBeVisible();
  const calls = api.trackingSources.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(api.trackingSources).toHaveBeenCalledTimes(calls);
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(api.trackingSources).toHaveBeenCalledTimes(calls + 1);
  unmount();
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(api.trackingSources).toHaveBeenCalledTimes(calls + 1);
  expect(api.trackingScanAll).not.toHaveBeenCalled();
});

test('闲置来源低频读取能发现设置触发的新任务，不自动提交扫描', async () => {
  vi.useFakeTimers();
  api.trackingSources.mockResolvedValueOnce({ sources: [{ root_id: 'r1', display_name: '本地新番', status: 'idle' }] })
    .mockResolvedValue({ sources: [{ root_id: 'r1', task_id: 'automatic-scan', display_name: '本地新番', status: 'running' }] });
  render(<CategoryPage />);
  await act(async () => {});
  expect(screen.getByText('尚未更新')).toBeVisible();
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(screen.getByText('正在更新')).toBeVisible();
  expect(api.trackingScanAll).not.toHaveBeenCalled();
});

test('迟到的页面初次读取不能覆盖手动刷新返回的排队状态', async () => {
  let finish!: (value: { sources: unknown[] }) => void;
  api.trackingSources.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  api.trackingScanAll.mockResolvedValue({ tasks: [{ root_id: 'r1', task_id: 'scan-1', status: 'queued', display_name: '本地新番' }] });
  render(<CategoryPage />);
  fireEvent.click(screen.getByRole('button', { name: '刷新新番' }));
  expect(await screen.findByText('等待更新')).toBeVisible();
  await act(async () => finish({ sources: [] }));
  expect(screen.getByText('等待更新')).toBeVisible();
});

test('新TXT需求只跳转文件选择，不把旧清单当更新来源扫描', async () => {
  api.trackingSources.mockResolvedValue({ sources: [{ root_id: 'txt-root', status: 'needs_new_txt', display_name: '纯TXT新番' }] });
  render(<CategoryPage />);
  fireEvent.click(await screen.findByRole('button', { name: '选择新导出 TXT' }));
  expect(api.goManageView).toHaveBeenCalledWith('overview');
  expect(JSON.parse(sessionStorage.getItem('kumiplayer.media-v4.ongoing-source-action') || '{}')).toEqual({ root_id: 'txt-root', action: 'new_txt' });
  expect(api.trackingScanAll).not.toHaveBeenCalled();
  expect(api.trackingCancelScan).not.toHaveBeenCalled();
});

test('PosterCard 显示追更标签与最新集摘要，未追更不显示', () => {
  const { rerender } = render(<PosterCard work={{ work_id: 'w1', title: '追更作品', media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: { work_id: 'w1', status: 'watching', note: '', favorite: false, updated_at: 'now' }, latest_episode_number: 5 } as never} index={0} />);
  expect(screen.getByText(/追更中 · 更新至 5 集/)).toBeVisible();

  rerender(<PosterCard work={{ work_id: 'w2', title: '普通作品', media_type: 'tv', show_type: 'anime_series', source: 'pan115', sources: ['pan115'], card_type: 'main_series', poster_path: '', fanart_path: '', clearlogo_path: '', dir_path: '', watch_status: null } as never} index={0} />);
  expect(screen.queryByText(/追更中/)).toBeNull();
});
