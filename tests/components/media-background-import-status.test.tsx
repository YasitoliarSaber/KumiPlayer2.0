import { render, screen } from '@testing-library/react';
import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { describe, expect, test } from 'vitest';
import MediaBackgroundImportStatus from '../../src/components/media/MediaBackgroundImportStatus';
import type { OpenListImportBatch } from '../../src/api/openlist';

const batch: OpenListImportBatch = {
  batch_id: 'batch-1',
  status: 'succeeded',
  mode: 'auto_safe',
  import_family: 'anime',
  created_at: '2026-08-13T00:00:00+08:00',
  updated_at: '2026-08-13T00:00:00+08:00',
  job_ids: ['scan-1'],
  roots: [{
    batch_id: 'batch-1', root_id: 'root-1', remote_locator: '/动画', normalized_locator: '/动画', local_locator: 'K:/动画',
    import_family: 'anime', import_scope: '', status: 'succeeded', generation: 1,
    job_id: 'scan-1', job_status: 'succeeded', progress: 100, message: '扫描完成',
    units: [
      { unit_id: 'unit-a', revision_id: 'rev-a', work_title: '作品 A', boundary: '/动画/作品 A', video_count: 12, discovery_status: 'plan_ready', state: 'completed' },
      { unit_id: 'unit-b', revision_id: '', work_title: '作品 B', boundary: '/动画/作品 B', video_count: 2, discovery_status: 'needs_review', state: 'needs_review' },
    ],
  }],
};

describe('后台媒体导入观察页', () => {
  test('复用执行窗口的阶段层级，并按真实下游 job 标示最终阶段', () => {
    render(<FluentProvider theme={webDarkTheme}><MediaBackgroundImportStatus batch={batch} source="openlist" /></FluentProvider>);

    expect(screen.getByRole('heading', { name: '媒体库已更新' })).toBeVisible();
    const stages = screen.getByRole('list', { name: '执行阶段' });
    expect(stages).toBeVisible();
    expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent('更新媒体库');
    expect(stages.querySelectorAll('.complete')).toHaveLength(4);
    expect(screen.getByText('作品 A')).toBeVisible();
    expect(screen.getByText('已完成')).toBeVisible();
    expect(screen.getByText('待处理识别结果')).toBeVisible();
    expect(screen.getByText(/不会阻塞其他作品/)).toBeVisible();
  });
});
