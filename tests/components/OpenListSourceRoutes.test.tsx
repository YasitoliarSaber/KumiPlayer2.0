import { describe, expect, test, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import OpenListSourceRoutes from '../../src/components/settings/OpenListSourceRoutes';
import type { OpenListRoute, ProviderId } from '../../src/api/types';
import type { OpenListRouteItem } from '../../src/api/openlist';

const routes: OpenListRoute[] = [
  {
    route_id: 'r1',
    label: '115网盘',
    remote_prefix: '/115网盘',
    provider_id: 'pan115',
    enabled: true,
    local_path: 'K:\\115网盘',
    local_available: false,
  },
  {
    route_id: 'r2',
    label: '夸克网盘',
    remote_prefix: '/夸克网盘',
    provider_id: 'quark',
    enabled: true,
    local_path: 'K:\\夸克网盘',
    local_available: false,
  },
];

const draft: OpenListRouteItem[] = routes.map((r) => ({
  route_id: r.route_id,
  label: r.label,
  remote_prefix: r.remote_prefix,
  provider_id: r.provider_id as ProviderId,
  enabled: r.enabled,
}));

function renderRoutes(props: Partial<Parameters<typeof OpenListSourceRoutes>[0]> = {}) {
  const defaults = {
    configured: true,
    routes,
    draft,
    discoverItems: [],
    notice: '',
    busy: null,
    onDiscover: vi.fn(async () => undefined),
    onSave: vi.fn(async () => undefined),
    onUpdateDraft: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  return {
    ...merged,
    ...render(<OpenListSourceRoutes {...merged} />),
  };
}

describe('OpenListSourceRoutes', () => {
  test('紧凑卡片渲染：名称 / 远端目录 / 内容提供商 / 推导路径', () => {
    renderRoutes();
    expect(screen.getByText('115网盘')).toBeTruthy();
    expect(screen.getByText('/115网盘')).toBeTruthy();
    expect(screen.getByText(/内容提供商：115 网盘/)).toBeTruthy();
    expect(screen.getByText('K:\\115网盘')).toBeTruthy();
    expect(screen.getByText('夸克网盘')).toBeTruthy();
    expect(screen.getByText('/夸克网盘')).toBeTruthy();
  });

  test('未配置时提示先保存 OpenList 连接', () => {
    renderRoutes({ configured: false });
    expect(screen.getByText(/请先保存 OpenList 连接/)).toBeTruthy();
  });

  test('点击“编辑”展开高级字段（显示名称/提供商/可作为媒体来源/只读路径）', () => {
    renderRoutes();
    fireEvent.click(screen.getAllByText('编辑')[0]);
    expect(screen.getByText('显示名称')).toBeTruthy();
    expect(screen.getByText('内容提供商')).toBeTruthy();
    expect(screen.getByText('可作为媒体来源')).toBeTruthy();
    expect(screen.getByText('远端路径（只读）')).toBeTruthy();
    expect(screen.getByText('推导路径（只读）')).toBeTruthy();
  });

  test('enabled 开关切换触发 onUpdateDraft', () => {
    const { onUpdateDraft } = renderRoutes();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[0]);
    expect(onUpdateDraft).toHaveBeenCalledWith('/115网盘', { enabled: false });
  });

  test('provider 修改触发 onUpdateDraft', () => {
    const { onUpdateDraft } = renderRoutes();
    fireEvent.click(screen.getAllByText('编辑')[0]);
    const select = screen.getAllByRole('combobox')[0];
    fireEvent.change(select, { target: { value: 'baidu' } });
    expect(onUpdateDraft).toHaveBeenCalledWith('/115网盘', { provider_id: 'baidu' });
  });

  test('label 修改触发 onUpdateDraft', () => {
    const { onUpdateDraft } = renderRoutes();
    fireEvent.click(screen.getAllByText('编辑')[0]);
    const input = screen.getByDisplayValue('115网盘');
    fireEvent.change(input, { target: { value: '115 网盘（主）' } });
    expect(onUpdateDraft).toHaveBeenCalledWith('/115网盘', { label: '115 网盘（主）' });
  });

  test('有修改时显示“有 N 项更改尚未保存”', () => {
    renderRoutes();
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    expect(screen.getByText(/有 1 项更改尚未保存/)).toBeTruthy();
    // 保存按钮可用
    expect((screen.getByText('保存更改') as HTMLButtonElement).disabled).toBe(false);
  });

  test('无修改时保存按钮禁用', () => {
    renderRoutes();
    expect((screen.getByText('保存更改') as HTMLButtonElement).disabled).toBe(true);
  });

  test('保存更改调用 onSave 并清除脏标记', async () => {
    const { onSave } = renderRoutes();
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByText('保存更改'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/有 1 项更改尚未保存/)).toBeNull();
  });

  test('刷新来源目录调用 onDiscover', () => {
    const { onDiscover } = renderRoutes();
    fireEvent.click(screen.getByText('刷新来源目录'));
    expect(onDiscover).toHaveBeenCalledTimes(1);
  });

  test('Save reject → 可见错误且无未处理 rejection', async () => {
    const { onSave } = renderRoutes({
      onSave: vi.fn(async () => { throw new Error('保存失败'); }),
    });
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByText('保存更改'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText('保存失败')).toBeTruthy();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  test('推导本地路径保留（未挂载时不显示错误）', () => {
    renderRoutes({ routes: routes.map((r) => ({ ...r, local_path: '' })) });
    // 无推导路径时卡片不显示空路径行，但名称与远端目录仍渲染
    expect(screen.getByText('115网盘')).toBeTruthy();
    expect(screen.getByText('/115网盘')).toBeTruthy();
  });
});
