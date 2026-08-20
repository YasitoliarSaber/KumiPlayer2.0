import { useState } from 'react';
import { Button } from '@fluentui/react-components';
import { openlistApi, type OpenListDiscoverItem, type OpenListRouteItem } from '../../api/openlist';
import type { OpenListRoute, ProviderId } from '../../api/types';

/**
 * OpenList 来源目录（路由）面板（OL-4：Settings IA）。
 *
 * 职责：
 * - 刷新来源目录（discover）与保存更改；
 * - 紧凑卡片：☑ 名称 / 远端目录 / 内容提供商 / 推导本地路径（只读）；
 * - 点击「编辑」展开高级字段（显示名称 / 内容提供商 / 可作为媒体来源开关）；
 * - 有未保存更改时提示「有 N 项更改尚未保存」。
 */
const ROUTE_PROVIDER_OPTIONS: Array<{ value: ProviderId; label: string }> = [
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'quark', label: '夸克网盘' },
  { value: 'other', label: '其他远程来源' },
];

interface OpenListSourceRoutesProps {
  configured: boolean;
  routes: OpenListRoute[];
  draft: OpenListRouteItem[];
  discoverItems: OpenListDiscoverItem[];
  notice: string;
  busy?: string | null;
  onDiscover: () => Promise<void>;
  onSave: () => Promise<void>;
  onUpdateDraft: (prefix: string, patch: Partial<OpenListRouteItem>) => void;
}

export default function OpenListSourceRoutes({
  configured,
  routes,
  draft,
  discoverItems,
  notice,
  busy,
  onDiscover,
  onSave,
  onUpdateDraft,
}: OpenListSourceRoutesProps) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [dirtyPrefixes, setDirtyPrefixes] = useState<Record<string, boolean>>({});
  const [actionLock, setActionLock] = useState<string | null>(null);
  const [localError, setLocalError] = useState('');

  const dirtyCount = Object.values(dirtyPrefixes).filter(Boolean).length;
  const isBusy = actionLock !== null || Boolean(busy);

  const toggleEdit = (prefix: string) => {
    setExpanded((current) => ({ ...current, [prefix]: !current[prefix] }));
  };

  const handleUpdate = (prefix: string, patch: Partial<OpenListRouteItem>) => {
    onUpdateDraft(prefix, patch);
    setDirtyPrefixes((current) => ({ ...current, [prefix]: true }));
  };

  const handleDiscover = async () => {
    if (actionLock) return; // 单操作锁：双击不产生并发
    setActionLock('refresh');
    setLocalError('');
    try {
      await onDiscover();
    } catch (error) {
      setLocalError((error as Error)?.message || '刷新来源目录失败');
    } finally {
      setActionLock(null);
    }
  };

  const handleSave = async () => {
    if (actionLock) return;
    setActionLock('save');
    setLocalError('');
    try {
      await onSave();
      setDirtyPrefixes({});
    } catch (error) {
      setLocalError((error as Error)?.message || '保存来源目录失败');
    } finally {
      setActionLock(null);
    }
  };

  const localPathFor = (prefix: string) =>
    routes.find((route) => route.remote_prefix === prefix)?.local_path ?? '';

  return (
    <div className="sources-routes-panel">
      <div className="sources-openlist-card-head">
        <strong>来源目录</strong>
        <span className="sources-route-summary">KumiPlayer 会读取 OpenList 顶层目录，你只需要确认每个目录来自哪个网盘。</span>
      </div>

      {!configured ? (
        <p className="settings-openlist-notice">请先保存 OpenList 连接，再刷新并配置来源目录。</p>
      ) : (
        <>
          <div className="sources-openlist-actions">
            <Button appearance="secondary" size="small" onClick={() => void handleDiscover()} className="settings-ghost-btn fluent-settings-btn" disabled={isBusy}>
              {actionLock === 'refresh' ? '处理中…' : '刷新来源目录'}
            </Button>
            <Button appearance="primary" size="small" onClick={() => void handleSave()} className="settings-primary-btn fluent-settings-btn" disabled={dirtyCount === 0 || isBusy}>
              {actionLock === 'save' ? '处理中…' : '保存更改'}
            </Button>
            {dirtyCount > 0 && <span className="sources-dirty-hint">有 {dirtyCount} 项更改尚未保存。</span>}
          </div>

          {(notice || localError) && <p className={`settings-openlist-notice${localError ? ' error' : ''}`}>{localError || notice}</p>}
          {draft.length === 0 && discoverItems.length === 0 && (
            <p className="settings-openlist-notice">尚未读取来源目录。点击「刷新来源目录」从 OpenList 顶层读取。</p>
          )}

          <div className="settings-route-list">
            {draft.map((route) => {
              const localPath = localPathFor(route.remote_prefix);
              const isExpanded = Boolean(expanded[route.remote_prefix]);
              const isDirty = Boolean(dirtyPrefixes[route.remote_prefix]);
              const providerLabel = ROUTE_PROVIDER_OPTIONS.find((option) => option.value === route.provider_id)?.label ?? route.provider_id;
              return (
                <div key={route.remote_prefix} className="sources-route-card">
                  <div className="sources-route-card-main">
                    <label className="sources-route-card-name" title="不作为媒体来源时取消勾选（仍可浏览）">
                      <input type="checkbox" checked={route.enabled} onChange={(event) => handleUpdate(route.remote_prefix, { enabled: event.target.checked })} />
                      <span>{route.label || route.remote_prefix}</span>
                    </label>
                    <div className="sources-route-card-paths">
                      <span>远端目录：<code>{route.remote_prefix}</code></span>
                      <span>内容提供商：{providerLabel}</span>
                      {localPath && <span>推导路径：<code>{localPath}</code></span>}
                    </div>
                    <Button appearance="secondary" size="small" onClick={() => toggleEdit(route.remote_prefix)} className="settings-ghost-btn fluent-settings-btn">
                      {isExpanded ? '收起' : '编辑'}
                    </Button>
                  </div>
                  {isDirty && <span className="sources-dirty-hint">该项有未保存的更改</span>}
                  {isExpanded && (
                    <div className="sources-route-card-edit">
                      <label className="settings-config-row">
                        <span>显示名称</span>
                        <input type="text" value={route.label} onChange={(event) => handleUpdate(route.remote_prefix, { label: event.target.value })} className="settings-input" />
                      </label>
                      <label className="settings-config-row">
                        <span>内容提供商</span>
                        <select value={route.provider_id} onChange={(event) => handleUpdate(route.remote_prefix, { provider_id: event.target.value as ProviderId })} className="settings-input">
                          {ROUTE_PROVIDER_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </select>
                      </label>
                      <label className="settings-route-enabled" title="不作为媒体来源时取消勾选（仍可浏览）">
                        <input type="checkbox" checked={route.enabled} onChange={(event) => handleUpdate(route.remote_prefix, { enabled: event.target.checked })} />
                        <span>可作为媒体来源</span>
                      </label>
                      <div className="settings-config-row settings-openlist-derived">
                        <span>远端路径（只读）</span>
                        <output>{route.remote_prefix}</output>
                      </div>
                      <div className="settings-config-row settings-openlist-derived">
                        <span>推导路径（只读）</span>
                        <output>{localPath || '填写挂载位置后自动推导'}</output>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
