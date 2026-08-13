import { useState } from 'react';
import { Button } from '@fluentui/react-components';
import { CheckCircle2, Circle, CircleX, LoaderCircle, Search, TriangleAlert, type LucideIcon } from 'lucide-react';

export type MediaLog = {
  time?: string;
  kind?: 'info' | 'done' | 'warn' | 'error' | 'search';
  message?: string;
};

const KIND_ICON: Record<NonNullable<MediaLog['kind']>, LucideIcon> = {
  done: CheckCircle2,
  warn: TriangleAlert,
  error: CircleX,
  search: Search,
  info: Circle,
};

function formatLogTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(11, 16);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

type Props = {
  logs: MediaLog[];
  limit?: number;
  ariaLabel: string;
  variant?: 'timeline' | 'work-summary';
};

type WorkLogGroup = {
  title: string;
  logs: MediaLog[];
  state: 'active' | 'complete' | 'attention';
};

function workTitleFromStart(message: string): string | null {
  const match = message.match(/^开始处理\s*[:：]\s*(.+)$/);
  return match?.[1]?.trim() || null;
}

function groupWorkLogs(logs: MediaLog[]): WorkLogGroup[] {
  const groups: WorkLogGroup[] = [];
  let current: WorkLogGroup | null = null;
  for (const log of logs) {
    const message = log.message || '执行中';
    const title = workTitleFromStart(message);
    if (title) {
      current = { title, logs: [], state: 'active' };
      groups.push(current);
    }
    if (!current) {
      current = { title: '任务准备与公共记录', logs: [], state: 'active' };
      groups.push(current);
    }
    current.logs.push(log);
    if (log.kind === 'error' || log.kind === 'warn') current.state = 'attention';
    if (/^完成作品\s*[:：]|^作品数据已更新\s*[:：]/.test(message) && current.state !== 'attention') {
      current.state = 'complete';
    }
  }
  return groups;
}

function workSummary(group: WorkLogGroup): string {
  const latest = group.logs.at(-1)?.message || '';
  if (group.state === 'attention') return '包含需要关注的记录';
  if (group.state === 'complete') return '资料补充完成';
  if (/^(搜索作品|找到候选|自动采用)\s*[:：]/.test(latest)) return '正在检索作品资料';
  if (/^正在刮削/.test(latest)) return '正在处理分集资料';
  if (/^(海报|背景图|Logo|剧集 NFO|刮削映射|TMDB 详情)完成\s*[:：]/.test(latest)) return '正在补充作品资料';
  return '正在整理作品资料';
}

function groupIcon(state: WorkLogGroup['state']) {
  if (state === 'complete') return <CheckCircle2 size={17} />;
  if (state === 'attention') return <TriangleAlert size={17} />;
  return <LoaderCircle size={17} />;
}

function WorkSummaryLogList({ logs, limit, ariaLabel }: Props) {
  const groups = groupWorkLogs(logs);
  const visibleGroups = limit && groups.length > limit ? groups.slice(-limit) : groups;
  return (
    <div className="media-work-log-list">
      <ol aria-label={ariaLabel}>
        {visibleGroups.map((group, groupIndex) => {
          const latest = group.logs.at(-1);
          const isOpen = group.state === 'active' || group.state === 'attention';
          return (
            <li className={`media-work-log-group ${group.state}`} key={`${group.title}-${groupIndex}`}>
              <div className="media-work-log-head">
                <span className="media-work-log-state" aria-hidden="true">{groupIcon(group.state)}</span>
                <div>
                  <strong>{group.title}</strong>
                  <span>{workSummary(group)}</span>
                </div>
                {latest?.time && <time dateTime={latest.time}>{formatLogTime(latest.time)}</time>}
              </div>
              <details open={isOpen}>
                <summary>{isOpen ? '收起详细记录' : `查看 ${group.logs.length} 条详细记录`}</summary>
                <ol>
                  {group.logs.map((log, index) => {
                    const kind = log.kind || 'info';
                    const Icon = KIND_ICON[kind];
                    return <li className={`media-log-item ${log.time ? 'has-timestamp' : 'no-timestamp'} ${kind}`} key={`${log.time || group.title}-${index}`}>
                      {log.time && <time dateTime={log.time}>{formatLogTime(log.time)}</time>}
                      <span className="media-log-icon" aria-hidden="true"><Icon size={14} /></span>
                      <span className="media-log-message">{log.message || '执行中'}</span>
                    </li>;
                  })}
                </ol>
              </details>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * 确认页与工作台共用的日志列表。
 * 有真实时间的执行活动渲染为 时间/图标/消息 三列；
 * 无时间的解析摘要渲染为 图标/消息 两列，不显示 --:-- 占位。
 * 组件自身不设 max-height 或滚动条，"查看全部/仅显示最近" 只控制渲染数量。
 */
export default function MediaLogList({ logs, limit, ariaLabel, variant = 'timeline' }: Props) {
  if (variant === 'work-summary') return <WorkSummaryLogList logs={logs} limit={limit} ariaLabel={ariaLabel} />;
  const [expanded, setExpanded] = useState(false);
  const effectiveLimit = limit ?? 30;
  const hasOverflow = logs.length > effectiveLimit;
  const visibleLogs = expanded || !hasOverflow ? logs : logs.slice(-effectiveLimit);

  return (
    <div className="media-log-list">
      <ol aria-label={ariaLabel}>
        {visibleLogs.map((log, index) => {
          const kind = log.kind || 'info';
          const Icon = KIND_ICON[kind];
          return (
            <li
              className={`media-log-item ${log.time ? 'has-timestamp' : 'no-timestamp'} ${kind}`}
              key={log.time ? `${log.time}-${index}` : `log-${index}`}
            >
              {log.time && <time dateTime={log.time}>{formatLogTime(log.time)}</time>}
              <span className="media-log-icon" aria-hidden="true"><Icon size={14} /></span>
              <span className="media-log-message">{log.message || '执行中'}</span>
            </li>
          );
        })}
      </ol>
      {hasOverflow && (
        <Button
          appearance="subtle"
          className="media-log-expand"
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? `仅显示最近 ${effectiveLimit} 条` : `查看全部 ${logs.length} 条记录`}
        </Button>
      )}
    </div>
  );
}
