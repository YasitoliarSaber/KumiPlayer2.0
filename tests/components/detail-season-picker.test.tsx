import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { describe, expect, test, vi } from 'vitest';
import DetailSeasonPicker from '../../src/components/media/DetailSeasonPicker';

const componentPath = join(process.cwd(), 'src', 'components', 'media', 'DetailSeasonPicker.tsx');

const seasons = [
  { key: 'season:1', label: '第 1 季' },
  { key: 'season:2', label: '第二季' },
  { key: 'special:0', label: '特别篇' },
];

function renderPicker(onSelect = vi.fn()) {
  const utils = render(
    <FluentProvider theme={webDarkTheme}>
      <div data-testid="outer-area" style={{ padding: 24 }}>
        <DetailSeasonPicker
          seasons={seasons}
          selectedKey="season:1"
          onSelect={onSelect}
        />
      </div>
    </FluentProvider>,
  );
  return { onSelect, ...utils };
}

describe('详情页季度选择器', () => {
  test('点击当前季度文字即可展开整个单选列表', () => {
    renderPicker();

    const trigger = screen.getByRole('combobox', { name: '选择季度' });
    expect(trigger).toHaveClass('detail-season-trigger');
    expect(trigger.parentElement).toHaveClass('detail-season-dropdown');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Clear selection', hidden: true })).not.toBeInTheDocument();

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const listbox = screen.getByRole('listbox', { name: '可选季度' });
    expect(listbox).toBeVisible();
    expect(within(trigger).getByLabelText('第 1 季')).toHaveClass('detail-season-label', 'detail-season-trigger-label');
    expect(within(listbox).getByLabelText('第二季')).toHaveClass('detail-season-label', 'detail-season-option-label');
  });

  test('选中另一个季度后提交唯一键并关闭列表', () => {
    const { onSelect } = renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: '第二季' }));

    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith('season:2');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  test('季度显示保留阿拉伯数字并压缩多余空格', () => {
    renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    expect(screen.getByLabelText('第 1 季')).toHaveTextContent('第1季');
    fireEvent.click(trigger);
    expect(screen.getByRole('option', { name: '第二季' })).toHaveTextContent('第2季');
  });

  test('再次单击同一个触发按钮可以收起展开的列表', () => {
    renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选季度' })).toBeVisible();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox', { name: '可选季度' })).not.toBeInTheDocument();
  });

  test('连续点击同一个季度按钮三轮：展开 → 收起 → 再展开', () => {
    renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox', { name: '可选季度' })).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选季度' })).toBeVisible();
  });

  test('真实指针事件序列下触发按钮自身切换，Fluent 回调不重复覆盖', () => {
    renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    // 真实浏览器点击链路：pointerdown → pointerup → click
    fireEvent.pointerDown(trigger);
    fireEvent.pointerUp(trigger);
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选季度' })).toBeVisible();

    // 第二轮真实指针事件序列，同一按钮必须直接收起（不能被 Fluent 回传与项目切换双重覆盖）
    fireEvent.pointerDown(trigger);
    fireEvent.pointerUp(trigger);
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox', { name: '可选季度' })).not.toBeInTheDocument();
  });

  test('契约：触发按钮自带项目 onClick 直接切换，onOpenChange 忽略触发器自身点击', () => {
    const source = readFileSync(componentPath, 'utf8');

    // 触发按钮必须提供项目自身的 onClick，用函数式状态更新直接切换（与三个点一致）
    expect(source).toMatch(/button=\{[\s\S]*?onClick: handleTriggerClick/);
    expect(source).toMatch(/const handleTriggerClick = \(\) => \{\s*setOpen\(\(current\) => !current\);\s*\};/);

    // onOpenChange 必须区分触发器自身 click 并忽略其回传的 data.open，
    // 避免 Fluent 内部 setOpen(event, !open) 与项目 onClick 在同一次点击中双重翻转
    expect(source).toMatch(/isTriggerClick/);
    expect(source).toMatch(/ev\?\.type === 'click'/);
    expect(source).toMatch(/triggerRef\.current!\.contains\(ev\.target as Node\)/);
    expect(source).toMatch(/if \(isTriggerClick\) \{\s*return;\s*\}/);
  });

  test('单击控件外部区域可以收起展开的列表', () => {
    const { getByTestId } = renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选季度' })).toBeVisible();

    fireEvent.click(getByTestId('outer-area'));
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox', { name: '可选季度' })).not.toBeInTheDocument();
  });

  test('按 Esc 可以收起展开的列表', () => {
    renderPicker();
    const trigger = screen.getByRole('combobox', { name: '选择季度' });

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选季度' })).toBeVisible();

    fireEvent.keyDown(trigger, { key: 'Escape' });

    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox', { name: '可选季度' })).not.toBeInTheDocument();
  });
});
