import { useEffect, useMemo, useState } from 'react';
import { Button } from '@fluentui/react-components';
import type { OpenListConfigPayload, OpenListTestResult } from '../../api/openlist';
import type { PublicConfig } from '../../api/config';

/**
 * OpenList 连接设置面板（OL-4：Settings IA）。
 *
 * 职责：
 * - 状态卡：登录信息已保存 / 连接正常 / 需要处理（准确文案，不暗示未验证为已连接）；
 * - 连接编辑区（默认折叠）：地址 / 远端根 / 挂载根 / 凭据 / WebDAV 辅助信息 / 高级设置；
 * - 按钮语义：未修改 → [检查连接]；remote-affecting 修改 → [验证并保存]；
 *   local-only 修改 → [保存设置]。
 */
export type OpenListConnectionState =
  | 'unconfigured'
  | 'saved_unverified'
  | 'checking'
  | 'connected'
  | 'credential_rejected'
  | 'root_permission_denied'
  | 'root_not_found'
  | 'network_unavailable'
  | 'rate_limited'
  | 'risk_control'
  | 'cooling_down';

export function stateLabel(state: OpenListConnectionState): string {
  switch (state) {
    case 'unconfigured':
      return '尚未配置';
    case 'saved_unverified':
      return '登录信息已保存，尚未检查当前连接';
    case 'checking':
      return '正在检查连接…';
    case 'connected':
      return 'OpenList 连接正常';
    case 'credential_rejected':
      return 'OpenList 拒绝了当前登录信息，请更新账号或密码';
    case 'root_permission_denied':
      return '已成功登录 OpenList，但当前远端根目录没有读取权限';
    case 'root_not_found':
      return '远端根目录不存在或已被移动';
    case 'network_unavailable':
      return '暂时无法访问 OpenList 服务，已保存的登录信息不会被删除';
    case 'rate_limited':
      return 'OpenList 请求过于频繁，请稍后再试';
    case 'risk_control':
    case 'cooling_down':
      return 'OpenList 请求已暂停，以避免继续触发访问保护';
    default:
      return '需要处理';
  }
}

export function buildOpenListWebdavAddress(serverUrl: string): string {
  const base = serverUrl.trim().replace(/\/+$/, '');
  if (!base) return '';
  const apiBase = base.toLowerCase().endsWith('/dav') ? base.slice(0, -4) : base;
  return `${apiBase}/dav/`;
}

// 非回环 http 才需要明文风险确认；localhost / 127.x / ::1 直接放行
export function isNonLoopbackHttp(url: string): boolean {
  if (!url.trim().toLowerCase().startsWith('http://')) return false;
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (host === 'localhost' || host === '::1' || host === '[::1]') return false;
    if (/^127\./.test(host)) return false;
    return true;
  } catch {
    return true;
  }
}

interface OpenListSettingsPanelProps {
  config: PublicConfig;
  draft: OpenListDraft;
  onChangeDraft: (key: keyof OpenListDraft, value: string) => void;
  /** 保存连接：resolve 表示成功；reject 会被面板收口为可见错误 */
  onSaveConnection: (payload: OpenListConfigPayload) => Promise<void>;
  /** 测试连接：返回后端 machine status code（不 throw 分类错误；网络异常才 reject） */
  onTestConnection: (payload: OpenListConfigPayload) => Promise<OpenListTestResult>;
  notice: string;
  noticeKind: 'success' | 'error' | 'info';
  /** 面板内部操作触发的提示（错误收口用）；外部 busy 锁 */
  onNotice?: (message: string, kind: 'success' | 'error' | 'info') => void;
  externalBusy?: string | null;
}

/** 后端 machine status code → 面板状态映射（REWORK：不得全部折叠为 network_unavailable） */
export function mapProbeCode(code: string): OpenListConnectionState {
  switch (code) {
    case 'connected':
      return 'connected';
    case 'credential_rejected':
      return 'credential_rejected';
    case 'root_permission_denied':
      return 'root_permission_denied';
    case 'root_not_found':
      return 'root_not_found';
    case 'rate_limited':
      return 'rate_limited';
    case 'risk_control':
    case 'cooling_down':
      return 'risk_control';
    case 'timeout':
    case 'network_unavailable':
    case 'server_unavailable':
      return 'network_unavailable';
    case 'invalid_configuration':
    case 'not_configured':
    case 'credential_store_unavailable':
      return 'unconfigured';
    case 'redirect_rejected':
    case 'unexpected_error':
    default:
      return 'network_unavailable';
  }
}

export interface OpenListDraft {
  server_url: string;
  remote_root: string;
  mount_root: string;
  username: string;
  password: string;
  cache_ttl: string;
  prefetch_limit: string;
}

/** remote-affecting 字段：变化时按钮语义为「验证并保存」 */
const REMOTE_AFFECTING_KEYS: Array<keyof OpenListDraft> = ['server_url', 'remote_root', 'username', 'password'];

export default function OpenListSettingsPanel({
  config,
  draft,
  onChangeDraft,
  onSaveConnection,
  onTestConnection,
  notice,
  noticeKind,
  onNotice,
  externalBusy,
}: OpenListSettingsPanelProps) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [allowOpenlistHttp, setAllowOpenlistHttp] = useState(false);
  const [actionLock, setActionLock] = useState<string | null>(null);
  // saved credential 初始状态必须是 saved_unverified，而不是 unconfigured（REWORK）
  const [probeState, setProbeState] = useState<OpenListConnectionState>(() =>
    config.openlist_configured ? 'saved_unverified' : 'unconfigured'
  );

  const saved = config.openlist_configured;
  const webdav = buildOpenListWebdavAddress(draft.server_url);

  // 当前 draft 相对「已保存配置」的修改状态（用于按钮语义）
  const dirtyKeys = useMemo(() => {
    const changed: Array<keyof OpenListDraft> = [];
    if (draft.server_url.trim() !== (config.openlist_server_url || '')) changed.push('server_url');
    if (draft.remote_root.trim() !== (config.openlist_remote_root || '/')) changed.push('remote_root');
    if (draft.mount_root.trim() !== (config.openlist_mount_root || '')) changed.push('mount_root');
    if (draft.username.trim() !== '') changed.push('username');
    if (draft.password !== '') changed.push('password');
    if (draft.cache_ttl !== String(config.openlist_cache_ttl_minutes ?? 1440)) changed.push('cache_ttl');
    if (draft.prefetch_limit !== String(config.openlist_prefetch_limit ?? 12)) changed.push('prefetch_limit');
    return changed;
  }, [draft, config]);

  const remoteAffectingDirty = dirtyKeys.some((key) => REMOTE_AFFECTING_KEYS.includes(key));
  const localOnlyDirty = dirtyKeys.length > 0 && !remoteAffectingDirty;
  const hasDirty = dirtyKeys.length > 0;

  // 未配置时暴露初始设置（用户名密码输入框可见）
  useEffect(() => {
    if (!saved) setCredentialsOpen(true);
  }, [saved]);

  const busy = actionLock !== null || Boolean(externalBusy);

  const buildPayload = (): OpenListConfigPayload => ({
    server_url: draft.server_url,
    remote_root: draft.remote_root,
    mount_root: draft.mount_root,
    username: draft.username,
    password: draft.password,
    // 非回环 HTTP 未确认时不得悄悄放行（REWORK P0：allow_insecure_http 必须来自风险确认）
    allow_insecure_http: allowOpenlistHttp || isNonLoopbackHttp(draft.server_url) === false,
    cache_ttl_minutes: Math.max(1, Number(draft.cache_ttl) || 1440),
    prefetch_limit: Math.max(0, Math.min(50, Number(draft.prefetch_limit) || 12)),
  });

  const handleSave = async () => {
    if (actionLock) return; // 单操作锁：双击不产生并发
    setActionLock('save');
    try {
      await onSaveConnection(buildPayload());
      // remote-affecting「验证并保存」成功后，后端已 Fresh Probe 成功 → connected；
      // local-only 保存不能凭空宣称连接正常（保持 saved_unverified）
      setProbeState(remoteAffectingDirty ? 'connected' : saved ? 'saved_unverified' : 'unconfigured');
    } catch (error) {
      setProbeState(remoteAffectingDirty ? 'unconfigured' : saved ? 'saved_unverified' : 'unconfigured');
      const message = (error as Error)?.message || 'OpenList 配置保存失败';
      onNotice?.(message, 'error');
    } finally {
      setActionLock(null);
    }
  };

  const handleTest = async () => {
    if (actionLock) return;
    setActionLock('test');
    setProbeState('checking');
    try {
      const result = await onTestConnection(buildPayload());
      // 真实 machine status code 映射，不得全部折叠为 network_unavailable
      setProbeState(result.ok ? 'connected' : mapProbeCode(result.code));
    } catch {
      setProbeState('network_unavailable');
    } finally {
      setActionLock(null);
    }
  };

  // 按钮语义：
  // - 无修改 → [检查连接]
  // - remote-affecting 修改 → [验证并保存]（后端 probe → save）
  // - 仅 local-only 修改 → [保存设置]（无需联网）
  const primaryLabel = !hasDirty
    ? '检查连接'
    : remoteAffectingDirty
      ? '验证并保存'
      : '保存设置';
  const primaryAction = !hasDirty ? handleTest : handleSave;

  return (
    <div className="sources-openlist-card">
      <div className="sources-openlist-card-head">
        <strong>OpenList</strong>
        <span className={`sources-openlist-status ${probeState === 'connected' ? 'is-ok' : probeState === 'unconfigured' ? '' : 'is-error'}`}>
          {stateLabel(probeState)}
        </span>
      </div>

      <div className="sources-openlist-meta">
        {config.openlist_server_url && <span>服务地址：<code>{config.openlist_server_url}</code></span>}
        {config.openlist_mount_root && <span>本地挂载：<code>{config.openlist_mount_root}</code></span>}
        {saved && <span>账号与密码已保存（仅存本机凭据管理器）</span>}
      </div>

      <div className="sources-openlist-actions">
        <Button appearance="secondary" size="small" onClick={() => setEditorOpen((v) => !v)} className="settings-ghost-btn fluent-settings-btn">
          {editorOpen ? '收起连接设置' : '管理连接'}
        </Button>
        <Button appearance="primary" size="small" onClick={primaryAction} className="settings-primary-btn fluent-settings-btn" disabled={Boolean(busy)}>
          {busy ? '处理中…' : primaryLabel}
        </Button>
      </div>

      {editorOpen && (
        <div className="sources-openlist-editor">
          <label className="settings-config-row">
            <span>OpenList 地址</span>
            <input type="url" value={draft.server_url} onChange={(event) => onChangeDraft('server_url', event.target.value)} className="settings-input" placeholder="http://localhost:5244" autoComplete="url" />
          </label>
          <div className="sources-webdav-hint">
            <span>WebDAV：</span>
            <code>{webdav ? `完整地址 ${webdav}` : '填写 OpenList 地址后自动生成'}</code>
          </div>
          <label className="settings-config-row">
            <span>远端根目录</span>
            <input type="text" value={draft.remote_root} onChange={(event) => onChangeDraft('remote_root', event.target.value)} className="settings-input" placeholder="/" />
          </label>
          <label className="settings-config-row">
            <span>本地挂载位置</span>
            <input type="text" value={draft.mount_root} onChange={(event) => onChangeDraft('mount_root', event.target.value)} className="settings-input" placeholder="K:\\" />
          </label>

          <div className="sources-credentials-block">
            {saved ? (
              <>
                <div className="sources-credentials-status">登录信息已保存</div>
                <Button appearance="secondary" size="small" onClick={() => setCredentialsOpen((v) => !v)} className="settings-ghost-btn fluent-settings-btn">
                  {credentialsOpen ? '收起账号密码' : '更新账号或密码'}
                </Button>
              </>
            ) : (
              <div className="sources-credentials-status">尚未保存登录信息</div>
            )}
            {(credentialsOpen || !saved) && (
              <div className="settings-field-list">
                <label className="settings-config-row">
                  <span>用户名</span>
                  <input type="text" value={draft.username} onChange={(event) => onChangeDraft('username', event.target.value)} className="settings-input" placeholder={saved ? '留空 = 使用已保存信息；填写 = 更新' : 'OpenList 用户名'} autoComplete="username" />
                </label>
                <label className="settings-config-row">
                  <span>密码</span>
                  <input type="password" value={draft.password} onChange={(event) => onChangeDraft('password', event.target.value)} className="settings-input" placeholder={saved ? '留空 = 使用已保存信息；填写 = 更新' : 'OpenList 密码'} autoComplete="current-password" />
                </label>
                <div className="sources-credentials-status">更换账号时需要同时输入该账号的密码。</div>
              </div>
            )}
          </div>

          <details className="sources-advanced">
            <summary>高级设置</summary>
            <div className="sources-advanced-body">
              <label className="settings-config-row">
                <span>目录浏览缓存（分钟）</span>
                <input type="number" min={1} max={43200} value={draft.cache_ttl} onChange={(event) => onChangeDraft('cache_ttl', event.target.value)} className="settings-input" placeholder="1440" />
              </label>
              <label className="settings-config-row">
                <span>提前加载子目录（上限 50）</span>
                <input type="number" min={0} max={50} value={draft.prefetch_limit} onChange={(event) => onChangeDraft('prefetch_limit', event.target.value)} className="settings-input" placeholder="12" />
              </label>
            </div>
          </details>

          {isNonLoopbackHttp(draft.server_url) && (
            <label className="settings-risk-confirm">
              <input type="checkbox" checked={allowOpenlistHttp} onChange={(event) => setAllowOpenlistHttp(event.target.checked)} />
              该地址是本地/局域网 HTTP，密码将以明文传输；勾选表示已知晓风险
            </label>
          )}

          {notice && <p className={`settings-openlist-notice${noticeKind !== 'info' ? ` ${noticeKind}` : ''}`}>{notice}</p>}
        </div>
      )}
    </div>
  );
}
