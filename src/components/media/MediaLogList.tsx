import { useState } from 'react';
import { Button } from '@fluentui/react-components';
import { CheckCircle2, Circle, CircleX, Search, TriangleAlert, type LucideIcon } from 'lucide-react';

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
};

/**
 * 确认页与工作台共用的日志列表。
 * 有真实时间的执行活动渲染为 时间/图标/消息 三列；
 * 无时间的解析摘要渲染为 图标/消息 两列，不显示 --:-- 占位。
 * 组件自身不设 max-height 或滚动条，"查看全部/仅显示最近" 只控制渲染数量。
 */
export default function MediaLogList({ logs, limit, ariaLabel }: Props) {
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
