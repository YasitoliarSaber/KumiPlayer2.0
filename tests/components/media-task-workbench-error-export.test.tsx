import { describe, expect, test } from 'vitest';
import { buildErrorLogExport } from '../../src/components/media/MediaTaskWorkbench';

describe('错误日志导出', () => {
  test('只导出警告与错误，并保留任务标题和原始信息', () => {
    const content = buildErrorLogExport('创建媒体库并补充资料', [
      { time: '2026-08-13T20:24:00+08:00', kind: 'done', message: '海报完成：作品 A' },
      { time: '2026-08-13T20:24:01+08:00', kind: 'warn', message: '跳过无效文件：作品 B' },
      { time: '2026-08-13T20:24:02+08:00', kind: 'error', message: 'TMDB 请求失败：作品 C' },
    ]);

    expect(content).toContain('任务：创建媒体库并补充资料');
    expect(content).toContain('[警告] 跳过无效文件：作品 B');
    expect(content).toContain('[错误] TMDB 请求失败：作品 C');
    expect(content).not.toContain('海报完成：作品 A');
  });

  test('任务终态错误没有日志条目时仍会被写入导出文件', () => {
    const content = buildErrorLogExport('补充资料', [], '请求超时');

    expect(content).toContain('[无时间] [错误] 请求超时');
  });
});
