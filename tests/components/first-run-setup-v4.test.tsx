import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import FirstRunSetup from '../../src/pages/FirstRunSetup';
import { pickFolder } from '../../src/platform/folderPicker';

const api = vi.hoisted(() => ({
  getMpvRuntime: vi.fn(async () => ({
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
  })),
  completeSetup: vi.fn(),
  testMpv: vi.fn(async () => ({ ok: true, message: '播放器可用' })),
}));

vi.mock('../../src/api/config', () => ({ configApi: api }));
vi.mock('../../src/platform/folderPicker', () => ({ pickFolder: vi.fn() }));

const config = {
  mirror_dir: '',
  pan115_root: '',
  baidu_root: '',
  local_root: '',
  directory_tree_dir: '',
};

describe('FirstRunSetup V4 引导', () => {
  beforeEach(() => vi.clearAllMocks());

  test('简化欢迎说明，并把 OpenList 明确为完成后的接入配置', async () => {
    render(<FirstRunSetup initialConfig={config as never} onComplete={vi.fn()} />);

    expect(screen.getByText(/不会移动或改名你的原始文件/)).toBeVisible();
    expect(screen.getByText(/配置可以随时修改/)).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('内置播放器已就绪')).toBeVisible();
    expect(screen.queryByText('mpv 0.40.0')).not.toBeInTheDocument();
    expect(screen.queryByText('x86_64-pc-windows-msvc')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('OpenList 可在完成后添加')).toBeVisible();
    expect(screen.getByText(/先选择一个本地或已挂载的媒体目录/)).toBeVisible();
  });

  test('先选来源类型，切换时保留每种来源的路径并一起提交', async () => {
    render(<FirstRunSetup initialConfig={{ ...config, mirror_dir: 'D:\\Mirror', local_root: 'D:\\Anime', pan115_root: 'H:\\115' } as never} onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByText('内置播放器已就绪');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(screen.getByRole('textbox', { name: '本地媒体根目录' })).toHaveValue('D:\\Anime');
    expect(screen.queryByRole('textbox', { name: '115 网盘挂载根目录' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /115 网盘/ }));
    fireEvent.change(screen.getByRole('textbox', { name: '115 网盘挂载根目录' }), { target: { value: 'H:\\Media' } });
    fireEvent.click(screen.getByRole('radio', { name: /本地文件夹/ }));
    expect(screen.getByRole('textbox', { name: '本地媒体根目录' })).toHaveValue('D:\\Anime');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(screen.queryByLabelText('TMDB API 读取访问令牌')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /海报与观看同步/ }));
    expect(screen.getByLabelText('TMDB API 读取访问令牌')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '验证并完成' }));
    await waitFor(() => expect(api.completeSetup).toHaveBeenCalledWith(expect.objectContaining({ local_root: 'D:\\Anime', pan115_root: 'H:\\Media' })));
    expect(api.completeSetup.mock.calls[0][0]).not.toHaveProperty('tmdb_bearer_token');
    expect(api.completeSetup.mock.calls[0][0]).not.toHaveProperty('bangumi_access_token');
  });

  test('文件夹选择失败时可见报错，手动输入仍然可用', async () => {
    vi.mocked(pickFolder).mockRejectedValueOnce(new Error('目录选择不可用'));
    render(<FirstRunSetup initialConfig={config as never} onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByText('内置播放器已就绪');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    fireEvent.click(screen.getByRole('button', { name: '选择本地媒体根目录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('目录选择不可用');
    fireEvent.change(screen.getByRole('textbox', { name: '本地媒体根目录' }), { target: { value: 'D:\\Anime' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续' })).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: '镜像目录（必填）' }), { target: { value: 'D:\\Mirror' } });
    expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
  });

  test('重新引导检查当前外部播放器，不将它误报为内置播放器', async () => {
    render(<FirstRunSetup initialConfig={{ ...config, player_mode: 'external', external_mpv_path: 'D:\\Player\\mpv.exe' } as never} onComplete={vi.fn()} mode="reconfigure" />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('外部播放器已就绪')).toBeVisible();
    expect(api.testMpv).toHaveBeenCalledWith('D:\\Player\\mpv.exe', 'external');
    expect(api.getMpvRuntime).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
  });

  test('外部播放器检测失败时阻止继续，重新检测成功后恢复', async () => {
    api.testMpv.mockResolvedValueOnce({ ok: false, message: '找不到播放器' });
    render(<FirstRunSetup initialConfig={{ ...config, player_mode: 'external', external_mpv_path: 'D:\\Player\\mpv.exe' } as never} onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('播放器暂不可用')).toBeVisible();
    expect(screen.getByText('找不到播放器')).toBeVisible();
    expect(screen.getByRole('button', { name: '继续' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重新检测' }));
    expect(await screen.findByText('外部播放器已就绪')).toBeVisible();
    expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
    expect(api.getMpvRuntime).not.toHaveBeenCalled();
  });

  test('旧版外部播放器配置沿用已保存的 mpv_path', async () => {
    render(<FirstRunSetup initialConfig={{ ...config, player_mode: 'external', external_mpv_path: '', mpv_path: 'D:\\Player\\mpv.exe' } as never} onComplete={vi.fn()} mode="reconfigure" />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('外部播放器已就绪')).toBeVisible();
    expect(api.testMpv).toHaveBeenCalledWith('D:\\Player\\mpv.exe', 'external');
    expect(api.getMpvRuntime).not.toHaveBeenCalled();
  });

  test('只有镜像目录不能进入下一步，至少需要一个实际媒体来源', async () => {
    render(<FirstRunSetup initialConfig={{ ...config, mirror_dir: 'D:\\Mirror' } as never} onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByText('内置播放器已就绪');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(screen.getByRole('button', { name: '继续' })).toBeDisabled();
    expect(screen.queryByLabelText('目录树文件目录（可选）')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /百度网盘/ }));
    fireEvent.change(screen.getByRole('textbox', { name: '百度网盘挂载位置' }), { target: { value: 'H:\\Baidu' } });
    expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
  });
});
