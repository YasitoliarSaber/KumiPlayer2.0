import { FluentProvider, webDarkTheme } from '@fluentui/react-components';
import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import MediaLogList from '../../src/components/media/MediaLogList';

describe('作品归组执行记录', () => {
  test('按开始处理边界归组，并将进行中的作品置于展开状态', () => {
    render(
      <FluentProvider theme={webDarkTheme}>
        <MediaLogList
          ariaLabel="执行记录"
          variant="work-summary"
          logs={[
            { time: '2026-08-13T20:24:00+08:00', kind: 'info', message: '开始处理：Angel Beats! / 2010' },
            { time: '2026-08-13T20:24:01+08:00', kind: 'done', message: '海报完成：天使的心跳！' },
            { time: '2026-08-13T20:24:02+08:00', kind: 'done', message: '完成作品：Angel Beats! / 2010 / 第 1 季' },
            { time: '2026-08-13T20:25:00+08:00', kind: 'info', message: '开始处理：CLANNAD / 第 1 季' },
            { time: '2026-08-13T20:25:01+08:00', kind: 'search', message: '搜索作品：CLANNAD' },
          ]}
        />
      </FluentProvider>,
    );

    expect(screen.getByText('Angel Beats! / 2010')).toBeVisible();
    expect(screen.getByText('CLANNAD / 第 1 季')).toBeVisible();
    expect(screen.getByText('资料补充完成')).toBeVisible();
    expect(screen.getByText('正在检索作品资料')).toBeVisible();
    expect(screen.getByText('搜索作品：CLANNAD')).toBeVisible();
  });
});
