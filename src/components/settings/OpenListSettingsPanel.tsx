import { useEffect, useMemo, useState } from 'react';
import { Button } from '@fluentui/react-components';
import { CheckCircle, Info, X, XCircle } from 'lucide-react';
import { openlistApi, type BindableProvider, type OpenListConfigPayload, type OpenListTestConnectionPayload, type OpenListTestResult, type OpenListTelemetrySummary } from '../../api/openlist';
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
  /** 保存连接：resolve 表示成功；reject 会被面板收口为可见错误。skipVerification=true 时跳过 Fresh Probe 直接持久化凭据。 */
  onSaveConnection: (payload: OpenListConfigPayload, skipVerification?: boolean) => Promise<void>;
  /** 测试连接：接收 TestConnection 专用 payload，返回后端 machine status code（不 throw 分类错误；网络异常才 reject） */
  onTestConnection: (payload: OpenListTestConnectionPayload) => Promise<OpenListTestResult>;
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
  const clearNotice = () => onNotice?.('', 'info');
  // 用户修改凭据/地址时自动清除旧错误提示，避免滞留
  const handleDraftChange = (key: keyof OpenListDraft, value: string) => {
    onChangeDraft(key, value);
    if (noticeKind === 'error') clearNotice();
  };
  const [editorOpen, setEditorOpen] = useState(false);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [allowOpenlistHttp, setAllowOpenlistHttp] = useState(false);
  const [actionLock, setActionLock] = useState<string | null>(null);
  const [telemetry, setTelemetry] = useState<OpenListTelemetrySummary | null>(null);
  // 登录失败后是否展示「仍然保存（不验证）」兑底入口
  const [skipVerificationOffered, setSkipVerificationOffered] = useState(false);
  // RWK-11：Provider root 绑定 OpenList 增量通道（最小可用入口）
  const [bindOpen, setBindOpen] = useState(false);
  const [bindProviders, setBindProviders] = useState<BindableProvider[]>([]);
  const [bindRootId, setBindRootId] = useState('');
  const [bindLocator, setBindLocator] = useState('');
  const [bindBusy, setBindBusy] = useState(false);
  const [bindMessage, setBindMessage] = useState('');

  const loadBindProviders = async () => {
    try {
      const data = await openlistApi.getBindableProviders();
      setBindProviders(data.providers || []);
      if (data.providers && data.providers.length > 0 && !bindRootId) {
        setBindRootId(data.providers[0].root_id);
      }
    } catch { /* 尽力而为 */ }
  };
  const toggleBindOpen = () => {
    const next = !bindOpen;
    setBindOpen(next);
    if (next) loadBindProviders();
  };

  const selectedProviderBaselineReady = bindProviders.find(
    (item) => item.root_id === bindRootId
  )?.baseline_ready ?? false;

  const runBindRoot = async () => {
    if (!bindRootId.trim() || !bindLocator.trim()) {
      setBindMessage('请填写来源根 ID 与 OpenList 远端目录');
      return;
    }
    setBindBusy(true);
    setBindMessage('');
    try {
      await openlistApi.bindRoot(bindRootId.trim(), bindLocator.trim());
      setBindMessage('绑定成功：该来源根现在可通过 OpenList 增量扫描');
    } catch (err) {
      setBindMessage('绑定失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setBindBusy(false);
    }
  };

  const runBoundRescan = async () => {
    if (!bindRootId.trim()) {
      setBindMessage('请填写已绑定的来源根 ID');
      return;
    }
    setBindBusy(true);
    setBindMessage('');
    try {
      const result = await openlistApi.rescanBoundRoot(bindRootId.trim());
      setBindMessage('增量扫描已排队（任务 ' + result.task_id + '），请到任务中心查看进度');
    } catch (err) {
      setBindMessage('增量扫描失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setBindBusy(false);
    }
  };


  useEffect(() => {
    let alive = true;
    openlistApi
      .getTelemetryToday()
      .then((data) => { if (alive) setTelemetry(data); })
      .catch(() => { /* 遥测展示尽力而为 */ });
    return () => { alive = false; };
  }, []);
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

  // Test Connection 专用 payload：后端 TestConnectionRequest 为 extra="forbid"，
  // 只发送其接受的字段（server_url/remote_root/username/password/allow_insecure_http）
  const buildTestPayload = () => {
    const full = buildPayload();
    return {
      server_url: full.server_url,
      remote_root: full.remote_root,
      username: full.username,
      password: full.password,
      allow_insecure_http: full.allow_insecure_http,
    };
  };

  const runSave = async (skipVerification: boolean) => {
    if (actionLock) return; // 单操作锁：双击不产生并发
    setActionLock(skipVerification ? 'save-skip' : 'save');
    // 清除旧提示，避免旧错误在重新保存时滞留
    onNotice?.('', 'info');
    setSkipVerificationOffered(false);
    try {
      await onSaveConnection(buildPayload(), skipVerification);
      if (skipVerification) {
        // 仅保存不验证：凭据已持久化，但连接尚未验证
        setProbeState('saved_unverified');
      } else {
        // remote-affecting「验证并保存」成功后，后端已 Fresh Probe 成功 → connected；
        // local-only 保存不能凭空宣称连接正常（保持 saved_unverified）
        setProbeState(remoteAffectingDirty ? 'connected' : saved ? 'saved_unverified' : 'unconfigured');
      }
    } catch (error) {
      // 候选保存失败：后端保持旧提交状态。已有保存配置 → saved_unverified
      // （不得显示 unconfigured 让用户误以为旧连接丢失）；首次配置失败 → unconfigured
      setProbeState(saved ? 'saved_unverified' : 'unconfigured');
      const message = (error as Error)?.message || 'OpenList 配置保存失败';
      onNotice?.(message, 'error');
      if (!skipVerification) setSkipVerificationOffered(true);
    } finally {
      setActionLock(null);
    }
  };

  const handleSave = () => runSave(false);
  const handleSaveSkipVerification = () => runSave(true);

  const handleTest = async () => {
    if (actionLock) return;
    setActionLock('test');
    setProbeState('checking');
    // 清除旧提示，避免旧错误在重新测试时滞留
    onNotice?.('', 'info');
    try {
      const result = await onTestConnection(buildTestPayload());
      // 真实 machine status code 映射，不得全部折叠为 network_unavailable
      const nextState = result.ok ? 'connected' : mapProbeCode(result.code);
      setProbeState(nextState);
      // 失败时显示后端返回的具体错误信息（如登录失败原因），而非空白
      if (!result.ok && result.message) {
        onNotice?.(result.message, 'error');
      }
    } catch {
      setProbeState('network_unavailable');
      onNotice?.('无法连接 OpenList 服务，请检查服务地址是否可达', 'error');
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
        <details className="sources-openlist-intro">
          <summary>介绍</summary>
          <span className="sources-route-summary">OpenList 用于读取远程目录；115、百度、夸克是实际内容来源。通常只需配置一次。</span>
        </details>
        {config.openlist_server_url && <span>服务地址：<code>{config.openlist_server_url}</code></span>}
        {config.openlist_mount_root && <span>本地挂载：<code>{config.openlist_mount_root}</code></span>}
        {saved && <span>账号与密码已保存（仅存本机凭据管理器）</span>}
      </div>

      {telemetry && (
        <div className="sources-openlist-telemetry">
          <span className="sources-telemetry-summary">
            今日请求：目录 {telemetry.fs_list} / 登录 {telemetry.login}（共 {telemetry.total}）
          </span>
          <span className="sources-route-hint">{telemetry.disclaimer}</span>
        </div>
      )}

      <div className="sources-openlist-binding">
        <Button appearance="secondary" size="small" onClick={toggleBindOpen} className="settings-ghost-btn fluent-settings-btn">
          {bindOpen ? '收起 Provider 增量绑定' : 'Provider 增量绑定（可选）'}
        </Button>
        <span className="sources-route-hint">TXT 导入完成本地基线后，挂载路径能对上 OpenList 挂载根与来源目录路由时会自动绑定增量通道；此处可查看状态或手动补绑</span>
        {bindOpen && (
          <div className="sources-openlist-binding-editor">
            <span className="sources-route-hint">
              绑定由本地路径反推导出（0 网络请求）；未自动绑定的来源可在此手动绑定。
            </span>
            <label className="settings-config-row">
              <span>115/百度来源</span>
              {bindProviders.length > 0 ? (
                <select value={bindRootId} onChange={(event) => setBindRootId(event.target.value)} className="settings-input">
                  {bindProviders.map((item) => (
                    <option key={item.root_id} value={item.root_id}>
                      {item.name}（{item.provider}）{item.bound ? ' · 已绑定' : ' · 未绑定'}
                      {item.baseline_ready
                        ? ` · 基线 ${item.baseline_directory_count} 目录 / ${item.baseline_node_count} 节点`
                        : ' · 尚未建立本地基线'}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="sources-route-hint">
                  暂无 115/百度来源。请先在媒体管理中导入目录树 TXT（导入后这里会出现来源）。
                </span>
              )}
            </label>
            <label className="settings-config-row">
              <span>OpenList 远端目录</span>
              <input type="text" value={bindLocator} onChange={(event) => setBindLocator(event.target.value)} className="settings-input" placeholder="/115网盘/动画" />
            </label>
            <div className="sources-openlist-actions">
              <Button appearance="secondary" size="small" onClick={runBindRoot} className="settings-ghost-btn fluent-settings-btn" disabled={bindBusy || !selectedProviderBaselineReady}>
                {selectedProviderBaselineReady ? '绑定 OpenList 增量' : '先完成本地基线'}
              </Button>
              <Button appearance="secondary" size="small" onClick={runBoundRescan} className="settings-ghost-btn fluent-settings-btn" disabled={bindBusy}>
                增量扫描
              </Button>
            </div>
            {bindMessage && <span className="sources-route-hint">{bindMessage}</span>}
          </div>
        )}
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
            <input type="url" value={draft.server_url} onChange={(event) => handleDraftChange('server_url', event.target.value)} className="settings-input" placeholder="http://localhost:5244" autoComplete="url" />
          </label>
          <div className="sources-webdav-hint">
            <span>WebDAV：</span>
            <code>{webdav ? `完整地址 ${webdav}` : '填写 OpenList 地址后自动生成'}</code>
          </div>
          <label className="settings-config-row">
            <span>远端根目录</span>
            <input type="text" value={draft.remote_root} onChange={(event) => handleDraftChange('remote_root', event.target.value)} className="settings-input" placeholder="/" />
          </label>
          <label className="settings-config-row">
            <span>本地挂载位置</span>
            <input type="text" value={draft.mount_root} onChange={(event) => handleDraftChange('mount_root', event.target.value)} className="settings-input" placeholder="K:\\" />
          </label>

          <div className="sources-credentials-block">
            {saved ? (
              <>
                <div className="sources-credentials-status">
                  登录信息已保存
                  {config.openlist_username_masked ? `（当前用户名：${config.openlist_username_masked}）` : ''}
                </div>
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
                  <input type="text" value={draft.username} onChange={(event) => handleDraftChange('username', event.target.value)} className="settings-input" placeholder={saved ? '留空 = 使用已保存信息；填写 = 更新' : 'OpenList 用户名'} autoComplete="username" />
                </label>
                <label className="settings-config-row">
                  <span>密码</span>
                  <input type="password" value={draft.password} onChange={(event) => handleDraftChange('password', event.target.value)} className="settings-input" placeholder={saved ? '留空 = 使用已保存信息；填写 = 更新' : 'OpenList 密码'} autoComplete="current-password" />
                </label>
                <div className="sources-credentials-status">更换用户名或密码时，需要同时填写新的用户名与密码。</div>
              </div>
            )}
            {skipVerificationOffered && (
              <div className="sources-openlist-actions">
                <Button appearance="secondary" size="small" onClick={handleSaveSkipVerification} className="settings-ghost-btn fluent-settings-btn" disabled={Boolean(busy)}>
                  仍然保存（不验证）
                </Button>
                <span className="sources-route-hint">登录验证失败时仍可先把账号密码保存下来，稍后再点「检查连接」确认。</span>
              </div>
            )}
          </div>

          <details className="sources-advanced">
            <summary>高级设置</summary>
            <div className="sources-advanced-body">
              <label className="settings-config-row">
                <span>目录浏览缓存（分钟）</span>
                <input type="number" min={1} max={43200} value={draft.cache_ttl} onChange={(event) => handleDraftChange('cache_ttl', event.target.value)} className="settings-input" placeholder="1440" />
              </label>
              <label className="settings-config-row">
                <span>提前加载子目录（上限 50）</span>
                <input type="number" min={0} max={50} value={draft.prefetch_limit} onChange={(event) => handleDraftChange('prefetch_limit', event.target.value)} className="settings-input" placeholder="12" />
              </label>
            </div>
          </details>

          {isNonLoopbackHttp(draft.server_url) && (
            <label className="settings-risk-confirm">
              <input type="checkbox" checked={allowOpenlistHttp} onChange={(event) => setAllowOpenlistHttp(event.target.checked)} />
              该地址是本地/局域网 HTTP，密码将以明文传输；勾选表示已知晓风险
            </label>
          )}

          {notice && (
            <p className={`settings-openlist-notice${noticeKind !== 'info' ? ` ${noticeKind}` : ''}`}>
              {noticeKind === 'success' ? <CheckCircle size={14} /> : noticeKind === 'error' ? <XCircle size={14} /> : <Info size={14} />}
              <span>{notice}</span>
              <button
                type="button"
                className="settings-openlist-notice-dismiss"
                aria-label="关闭提示"
                onClick={() => onNotice?.('', 'info')}
              >
                <X size={12} />
              </button>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
