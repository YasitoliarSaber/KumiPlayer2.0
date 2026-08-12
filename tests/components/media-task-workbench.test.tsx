import { render, screen } from '@testing-library/react';
import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { describe, expect, test, vi } from 'vitest';
import MediaTaskWorkbench from '../../src/components/media/MediaTaskWorkbench';
import type { TaskRecord } from '../../src/api/types';

function makeTask(overrides: Partial<TaskRecord> = {}): TaskRecord {
  return {
    task_id: 'task-1',
    task_type: 'mirror_generate',
    source: 'local',
    status: 'running',
    progress: 0,
    message: '',
    created_at: '2026-08-04T10:00:00+08:00',
    started_at: '2026-08-04T10:00:00+08:00',
    finished_at: '',
    error: '',
    result: null,
    ...overrides,
  };
}

function renderWorkbench(props: Partial<React.ComponentProps<typeof MediaTaskWorkbench>> = {}) {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MediaTaskWorkbench
        mode="mirror"
        title="创建媒体库并补充资料"
        description="根据确认内容生成媒体库并自动补充资料。"
        task={null}
        logs={[]}
        onStart={vi.fn()}
        startLabel="创建媒体库"
        disabled={false}
        {...props}
      />
    </FluentProvider>,
  );
}

describe('第三阶段媒体工作台', () => {
  test('四个子阶段、任务统计与执行记录常驻可见', () => {
    renderWorkbench();

    const stages = screen.getByRole('list', { name: '执行阶段' });
    expect(stages).toBeVisible();
    expect(stages.textContent).toContain('路径抽样验证');
    expect(stages.textContent).toContain('镜像生成');
    expect(stages.textContent).toContain('镜像结果校验');
    expect(stages.textContent).toContain('资料匹配/下载');
    expect(screen.getByRole('region', { name: '任务统计' })).toBeVisible();
    expect(screen.getByRole('region', { name: '执行记录' })).toBeVisible();
    expect(screen.getByRole('button', { name: '创建媒体库' })).toBeEnabled();
  });

  test('镜像失败显示重试入口，不出现人工继续按钮', () => {
    renderWorkbench({
      task: makeTask({
        task_type: 'mirror_generate',
        status: 'failed',
        progress: 100,
        message: '路径不可达',
        error: '路径不可达',
        result: { generated_count: 0, failed_count: 3, skipped_count: 0, items_count: 3 },
      }),
    });

    expect(screen.getByText('镜像生成失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重新创建' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '继续补充资料' })).not.toBeInTheDocument();
  });

  test('镜像完整后显示自动补充资料状态，不再要求确认镜像完成', () => {
    renderWorkbench({
      task: makeTask({
        task_type: 'mirror_generate',
        status: 'succeeded',
        progress: 100,
        result: { generated_count: 2, failed_count: 0, skipped_count: 1, items_count: 3 },
      }),
    });

    expect(screen.getByText('镜像已就绪')).toBeInTheDocument();
    expect(screen.getByText('正在自动开始补充资料…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续补充资料' })).not.toBeInTheDocument();
  });

  test('刮削停止后保留继续补充资料入口', () => {
    renderWorkbench({
      mode: 'scrape',
      task: makeTask({
        task_type: 'scrape_auto',
        status: 'cancelled',
        progress: 40,
        result: { auto_scraped: 1, total_targets: 3 },
      }),
      startLabel: '开始补充资料',
    });

    expect(screen.getByText('已停止')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续补充资料' })).toBeEnabled();
  });

  test('刮削完成后可导入新目录', () => {
    renderWorkbench({
      mode: 'scrape',
      task: makeTask({
        task_type: 'scrape_auto',
        status: 'succeeded',
        progress: 100,
        result: { auto_scraped: 2, skipped_existing: 1, total_targets: 3 },
      }),
      startLabel: '开始补充资料',
      onNewImport: vi.fn(),
    });

    expect(screen.getByText('资料补充完成')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导入新目录' })).toBeEnabled();
  });
});
