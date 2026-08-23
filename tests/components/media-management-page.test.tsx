import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import MediaManagementPage from '../../src/pages/MediaManagementPage';

const api = vi.hoisted(() => ({
  scan: vi.fn(),
  preview: vi.fn(),
  confirm: vi.fn(),
  status: vi.fn(),
  overrideEvidence: vi.fn(),
}));

vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: api }));
vi.mock('../../src/platform/folderPicker', () => ({
  pickDirectoryTreeFile: vi.fn(),
  pickFolder: vi.fn(),
}));
vi.mock('../../src/stores/mediaWorkflow', () => ({
  useMediaWorkflowStore: (selector: (state: unknown) => unknown) => selector({
    pendingDroppedTreePath: '',
    consumeDroppedTreePath: vi.fn(),
  }),
}));

beforeEach(() => {
  localStorage.clear();
  api.scan.mockReset();
  api.preview.mockReset();
  api.confirm.mockReset();
  api.status.mockReset();
  api.overrideEvidence.mockReset();
  api.scan.mockResolvedValue({ root_id: 'root-local', scan_id: 'scan-1', entries: [] });
  api.preview.mockResolvedValue({
    revision_id: 'rev-preview',
    status: 'draft',
    works: [],
    episodes: [],
    work_assets: [],
    issues: [],
  });
});

test('导入工作台用可见来源卡和连续步骤呈现主流程', () => {
  render(<MediaManagementPage />);

  expect(screen.getByRole('navigation', { name: '导入步骤' })).toBeVisible();
  expect(screen.getByRole('button', { name: '本地目录' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: '目录树 TXT' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByRole('button', { name: 'OpenList' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByRole('button', { name: '目录树 + OpenList 增量' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByRole('button', { name: '扫描并识别' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: '重新开始' })).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '媒体目录' })).toHaveAttribute('name', 'media_path');
  expect(screen.getByRole('textbox', { name: '媒体目录' })).toHaveAttribute('autocomplete', 'off');
});

test('切换来源会清空不兼容路径并只展示当前来源字段', () => {
  render(<MediaManagementPage />);

  const localPath = screen.getByRole('textbox', { name: '媒体目录' });
  fireEvent.change(localPath, { target: { value: 'D:\\Anime' } });
  expect(screen.getByRole('button', { name: '扫描并识别' })).toBeEnabled();

  fireEvent.click(screen.getByRole('button', { name: '目录树 TXT' }));
  expect(screen.getByRole('textbox', { name: '目录树 TXT 文件' })).toHaveValue('');
  const providerSelect = screen.getByRole('combobox', { name: '存储来源' });
  expect(providerSelect).toBeVisible();
  expect(providerSelect.tagName).toBe('SELECT');
  expect(providerSelect).toHaveAttribute('name', 'storage_provider');
  expect(screen.getByRole('textbox', { name: '本地挂载根目录（可选）' })).toBeVisible();
  expect(screen.getByRole('button', { name: '选择文件' })).toBeVisible();

  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }));
  expect(screen.queryByRole('textbox', { name: '目录树 TXT 文件' })).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: 'OpenList 远端目录' })).toHaveValue('');
  expect(screen.getByRole('button', { name: '扫描并识别' })).toBeEnabled();
});

test('混合来源用 TXT 建立基线并绑定后续 OpenList 增量目录', async () => {
  render(<MediaManagementPage />);

  fireEvent.click(screen.getByRole('button', { name: '目录树 + OpenList 增量' }));
  fireEvent.change(screen.getByRole('textbox', { name: '首次目录树 TXT 文件' }), {
    target: { value: 'D:\\Lists\\anime.txt' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: 'OpenList 增量目录' }), {
    target: { value: '/Anime' },
  });
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }));

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith({
    source: 'hybrid',
    root_path: '/Anime',
    tree_file: 'D:\\Lists\\anime.txt',
    provider: 'local',
    source_root: '',
    scan_mode: 'auto',
  }));
});

test('本地来源路径有效后提交统一 V4 扫描请求', async () => {
  render(<MediaManagementPage />);

  fireEvent.change(screen.getByRole('textbox', { name: '媒体目录' }), {
    target: { value: 'D:\\Anime' },
  });
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }));

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith({
    source: 'local',
    root_path: 'D:\\Anime',
    tree_file: '',
    provider: 'local',
    source_root: '',
    scan_mode: 'auto',
  }));
  expect(screen.getByRole('heading', { name: '检查识别结果' })).toBeVisible();
});

test('OpenList 可显式要求本次完整远端校验', async () => {
  render(<MediaManagementPage />);

  fireEvent.click(screen.getByRole('button', { name: 'OpenList' }));
  expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument();
  expect(screen.getByText('通常只检查新增和变化内容；发现结果不完整时再使用完整扫描。')).toBeVisible();
  expect(screen.getByText('增量扫描')).toBeVisible();
  fireEvent.click(screen.getByRole('switch', { name: '完整扫描' }));
  expect(screen.getByText('完整扫描')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '扫描并识别' }));

  await waitFor(() => expect(api.scan).toHaveBeenCalledWith(expect.objectContaining({
    source: 'openlist',
    scan_mode: 'full',
  })));
});
