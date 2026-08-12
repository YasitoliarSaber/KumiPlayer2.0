import { render, screen } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import AppErrorBoundary from '../../src/components/errors/AppErrorBoundary';
import RecoveryView from '../../src/components/errors/RecoveryView';

vi.mock('../../src/platform/desktopRecovery', () => ({
  isDesktopRuntime: () => false,
  openDesktopLogDirectory: vi.fn(),
  restartDesktopBackend: vi.fn(),
  saveDesktopDiagnostics: vi.fn(),
}));

test('浏览器预览中的恢复界面保留重载动作并禁用桌面专属动作', () => {
  render(<RecoveryView title="启动失败" message="后端未就绪" />);

  expect(screen.getByRole('heading', { name: '启动失败' })).toBeVisible();
  expect(screen.getByRole('button', { name: /重新加载/ })).toBeEnabled();
  expect(screen.getByRole('button', { name: /重启后端/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /打开日志/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /导出诊断/ })).toBeDisabled();
});

test('Error Boundary 捕获渲染异常并显示恢复动作', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);

  function BrokenView() {
    throw new Error('render failed');
  }

  render(<AppErrorBoundary><BrokenView /></AppErrorBoundary>);

  expect(screen.getByRole('heading', { name: '界面运行异常' })).toBeVisible();
  expect(screen.getByText(/媒体文件和媒体库数据没有被修改/)).toBeVisible();
  expect(screen.getByRole('button', { name: /重新加载/ })).toBeEnabled();
});
