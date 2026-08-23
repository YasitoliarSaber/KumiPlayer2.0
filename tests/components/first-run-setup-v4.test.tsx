import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import FirstRunSetup from '../../src/pages/FirstRunSetup';

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
  test('说明统一来源链路，并把 OpenList 明确为完成后的接入配置', async () => {
    render(<FirstRunSetup initialConfig={config as never} onComplete={vi.fn()} />);

    expect(screen.getByText(/SourceEvidence/)).toBeVisible();
    expect(screen.getByText(/来源先记录，再整理/)).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('内置播放器已就绪')).toBeVisible();
    expect(screen.queryByText('mpv 0.40.0')).not.toBeInTheDocument();
    expect(screen.queryByText('x86_64-pc-windows-msvc')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('OpenList 可在完成后添加')).toBeVisible();
    expect(screen.getByText(/并不替代首次配置的可访问媒体根目录/)).toBeVisible();
  });
});
