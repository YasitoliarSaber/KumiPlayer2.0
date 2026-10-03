import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Button, Spinner } from '@fluentui/react-components';
import {
  Database,
  ExternalLink,
  KeyRound,
  Network,
  Palette,
  PlaySquare,
  RotateCcw,
  UserRound,
  type LucideIcon,
} from 'lucide-react';
import { useBangumiStore } from '../stores/bangumi';
import { buildBangumiImageUrl } from '../api/bangumi';
import { useLibraryStore } from '../stores/library';
import { configApi, type MediaPathValidationResponse, type MpvRuntimeStatus, type PublicConfig } from '../api/config';
import { openlistApi, type OpenListConfigPayload, type OpenListDiscoverItem, type OpenListRouteItem } from '../api/openlist';
import type { OpenListRoute, ProviderId } from '../api/types';
import { tasksApi } from '../api/tasks';
import type { TaskRecord } from '../api/types';
import { useUiStore, type AppearanceMode } from '../stores/ui';
import { BANGUMI_ACCESS_TOKEN_URL, getTmdbCredentialError, TMDB_API_SETTINGS_URL } from '../config/credentials';
import DecodedImage from '../components/ui/DecodedImage';
import OpenListSettingsPanel, { type OpenListDraft } from '../components/settings/OpenListSettingsPanel';
import OpenListConnectionPicker from '../components/settings/OpenListConnectionPicker';
import FolderPathInput from '../components/settings/FolderPathInput';
import PlayerTuningPage from './PlayerTuningPage';
import { openlistConnections, selectOpenlistConnection } from '../api/openlistConnections';
import OpenListSourceRoutes from '../components/settings/OpenListSourceRoutes';
import OngoingUpdateSettingsPanel from '../components/settings/OngoingUpdateSettingsPanel';
import '../styles/settings-media-sources.css';
import '../styles/settings-navigation.css';
type SettingsTab = 'appearance' | 'sources' | 'openlist' | 'scrape' | 'player' | 'bangumi';
type SourceKey = 'pan115' | 'baidu' | 'quark' | 'local';

const sectionTabs: Array<{ key: SettingsTab; label: string; icon: LucideIcon }> = [
  { key: 'bangumi', label: '账户与同步', icon: UserRound },
  { key: 'appearance', label: '外观', icon: Palette },
  { key: 'sources', label: '媒体来源', icon: Database },
  { key: 'openlist', label: 'WebDAV 设置', icon: Network },
  { key: 'scrape', label: '元数据与图片', icon: KeyRound },
  { key: 'player', label: '播放', icon: PlaySquare },
];

const sourceLabels: Record<SourceKey | 'all' | 'openlist', string> = {
  all: '全部来源',
  pan115: '115 网盘',
  baidu: '百度网盘',
  quark: '夸克网盘',
  openlist: 'OpenList 连接',
  local: '本地',
};

const sectionId = (key: SettingsTab) => `settings-panel-${key}`;


export default function SettingsPage({ onOpenSetup }: { onOpenSetup?: () => void }) {
  const {
    user,
    isLoggedIn,
    hasStoredCredential,
    credentialState,
    authStatus,
    connectivity,
    lastSuccessAt,
    loading: bangumiLoading,
    error: bangumiError,
    sessionStatus: bangumiSessionStatus,
    restoreSession: restoreBangumiSession,
    verifySession: verifyBangumiSession,
    setToken,
    clearToken,
  } = useBangumiStore();
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const { appearanceMode, setAppearanceMode, ongoingCategoryName = '新番', setOngoingCategoryName } = useUiStore();
  const [activeSection, setActiveSection] = useState<SettingsTab>('bangumi');
  const [baseConfig, setConfig] = useState<PublicConfig | null>(null);
  const [selectedConnectionId, setSelectedConnectionId] = useState('legacy');
  const [connectionBusy, setConnectionBusy] = useState('');
  const config = useMemo(() => baseConfig ? selectOpenlistConnection(baseConfig, selectedConnectionId) : null, [baseConfig, selectedConnectionId]);
  const selectedConnectionRef = useRef(selectedConnectionId);
  selectedConnectionRef.current = selectedConnectionId;
  const [configLoading, setConfigLoading] = useState(false);
  const [mpvRuntime, setMpvRuntime] = useState<MpvRuntimeStatus | null>(null);
  const [operationMessage, setOperationMessage] = useState('');
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [mediaPathValidation, setMediaPathValidation] = useState<MediaPathValidationResponse | null>(null);
  const [bangumiToken, setBangumiToken] = useState('');
  const [openlistNotice, setOpenlistNotice] = useState('');
  const [openlistNoticeKind, setOpenlistNoticeKind] = useState<'success' | 'error' | 'info'>('info');
  const [openlistDraft, setOpenlistDraft] = useState<OpenListDraft>({
    server_url: '', remote_root: '/', mount_root: '', username: 'admin', password: '',
    cache_ttl: '1440', prefetch_limit: '12',
  });
  const [openlistRoutes, setOpenlistRoutes] = useState<OpenListRoute[]>([]);
  const [routeDraft, setRouteDraft] = useState<OpenListRouteItem[]>([]);
  const [routeDiscoverItems, setRouteDiscoverItems] = useState<OpenListDiscoverItem[]>([]);
  const [routeNotice, setRouteNotice] = useState('');
  const [routesLoaded, setRoutesLoaded] = useState(false);
  const routeAutoDiscoverRef = useRef(false);
  const seenLibraryRefreshRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    void loadCore();
  }, []);

  useEffect(() => {
    if (activeSection !== 'scrape') return;
    const timer = window.setInterval(() => {
      void refreshTasks(12).catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [activeSection]);

  // 打开「账户与同步」时**自动验证**一次：已保存登录信息时不该出现
  // "显示已连接却又提示尚未验证"这种自相矛盾的状态（用户反馈）。
  const bangumiAutoVerifiedRef = useRef(false);
  useEffect(() => {
    if (activeSection !== 'bangumi') return;
    if (!hasStoredCredential || authStatus !== 'unknown' || bangumiAutoVerifiedRef.current) return;
    bangumiAutoVerifiedRef.current = true;
    void Promise.resolve(verifyBangumiSession()).catch(() => undefined);
  }, [activeSection, hasStoredCredential, authStatus, verifyBangumiSession]);

  useEffect(() => {
    if (!config) return;
    setOpenlistDraft({
      name: config.openlist_connection_name || '默认连接',
      server_url: config.openlist_server_url,
      remote_root: config.openlist_remote_root || '/',
      mount_root: config.openlist_mount_root,
      username: config.openlist_configured ? '' : 'admin',
      password: '',
      cache_ttl: String(config.openlist_cache_ttl_minutes ?? 1440),
      prefetch_limit: String(config.openlist_prefetch_limit ?? 12),
    });
  }, [selectedConnectionId, config?.openlist_connection_name, config?.openlist_server_url, config?.openlist_remote_root, config?.openlist_mount_root, config?.openlist_configured, config?.openlist_cache_ttl_minutes, config?.openlist_prefetch_limit]);

  // 刷新来源目录（discover）：远端顶层目录 + 提供商建议合并进可编辑草稿。
  // auto=true 为启动时的自动读取（文案引导直接保存），false 为手动点击刷新。
  async function discoverRoutes(auto: boolean) {
    const identity = selectedConnectionId;
    const result = await openlistApi.discoverRoutes(identity);
    if (identity !== selectedConnectionRef.current) return;
    setRouteDiscoverItems(result.items);
    const saved = new Map(routeDraft.map((item) => [item.remote_prefix, item]));
    const discovered: OpenListRouteItem[] = result.items.map((item) => {
      const existing = saved.get(item.remote_prefix);
      if (existing) return existing;
      return {
        route_id: '',
        label: item.current_label || item.name,
        remote_prefix: item.remote_prefix,
        provider_id: item.current_provider || item.hint_provider,
        enabled: true,
      };
    });
    // 临时不可见或位于子目录的已保存来源不因刷新被移除；这里只补充新目录。
    const knownPrefixes = new Set(discovered.map((item) => item.remote_prefix));
    setRouteDraft([...discovered, ...routeDraft.filter((item) => !knownPrefixes.has(item.remote_prefix))]);
    setRouteNotice(
      auto
        ? `已读取 ${result.items.length} 个目录，请确认网盘类型后保存。`
        : `已刷新 ${result.items.length} 个目录，原有选择已保留。`,
    );
  }

  // 提供商路由：读取已保存路由
  useEffect(() => {
    if (!config?.openlist_configured) return;
    let cancelled = false;
    setRoutesLoaded(false);
    setRouteDiscoverItems([]);
    routeAutoDiscoverRef.current = false;
    void openlistApi.getRoutes(selectedConnectionId)
      .then((result) => {
        if (cancelled) return;
        setOpenlistRoutes(result.routes);
        setRouteDraft(result.routes.map((route) => ({
          route_id: route.route_id,
          label: route.label,
          remote_prefix: route.remote_prefix,
          provider_id: route.provider_id,
          enabled: route.enabled,
        })));
      })
      .catch(() => {
        if (!cancelled) setRouteNotice('读取来源路由失败，请确认 OpenList 连接配置');
      })
      .finally(() => {
        if (!cancelled) setRoutesLoaded(true);
      });
    return () => { cancelled = true; };
  }, [selectedConnectionId, config?.openlist_configured, config?.openlist_server_url, config?.openlist_remote_root]);

  // 已配置连接但没有任何已保存路由时，自动读取一次顶层目录建议；
  // 失败静默（保留手动「刷新来源目录」入口），避免每次重启都要求手动刷新。
  useEffect(() => {
    if (activeSection !== 'openlist' || !config?.openlist_configured || !routesLoaded || routeAutoDiscoverRef.current) return;
    if (openlistRoutes.length > 0 || routeDraft.length > 0 || routeDiscoverItems.length > 0) {
      routeAutoDiscoverRef.current = true;
      return;
    }
    routeAutoDiscoverRef.current = true;
    void discoverRoutes(true).catch(() => undefined);
  }, [activeSection, config?.openlist_configured, routesLoaded, openlistRoutes.length, routeDraft.length, routeDiscoverItems.length]);


  const selectSection = (key: SettingsTab) => {
    setActiveSection(key);
    document.querySelector<HTMLElement>('.app-main')?.scrollTo({ top: 0, behavior: 'auto' });
  };

  const report = (message: string) => {
    setOperationMessage(message);
    console.debug(`[settings] ${message}`);
  };

  const runAction = async (label: string, action: () => Promise<void>) => {
    if (activeAction) return;
    setActiveAction(label);
    try {
      report(`${label}中...`);
      await action();
    } catch (err) {
      report(`${label}失败：${(err as Error).message}`);
    } finally {
      setActiveAction(null);
    }
  };

  const loadCore = async () => {
    await Promise.allSettled([
      loadConfig(),
      loadMpvRuntime(),
      loadLibrary({ force: false }),
      refreshTasks(10),
    ]);
  };

  const loadMpvRuntime = async () => {
    try {
      setMpvRuntime(await configApi.getMpvRuntime());
    } catch {
      setMpvRuntime(null);
    }
  };

  const loadConfig = async () => {
    setConfigLoading(true);
    try {
      setConfig(await configApi.getConfig());
    } finally {
      setConfigLoading(false);
    }
  };

  const refreshTasks = async (limit = 12, source?: string) => {
    const result = await tasksApi.list({ limit, source });
    const tasks = result.tasks || [];
    if (tasks.some((task) => consumeLibraryRefreshMarker(task, seenLibraryRefreshRef.current))) {
      await loadLibrary({ force: true });
    }
    return tasks;
  };

  const saveConfig = async (patch: Partial<PublicConfig>) => {
    setConfigLoading(true);
    try {
      const updated = await configApi.patchConfig(patch);
      setConfig(updated);
      report('配置已保存');
    } catch (err) {
      report(`保存失败：${(err as Error).message}`);
    } finally {
      setConfigLoading(false);
    }
  };

  const saveTmdbCredential = (value: string) => {
    const credentialError = getTmdbCredentialError(value);
    if (credentialError) {
      report(credentialError);
      return;
    }
    void saveConfig({ tmdb_bearer_token: value.trim() });
  };

  const loginBangumi = () => runAction('Bangumi 登录', async () => {
    if (!bangumiToken.trim()) throw new Error('请输入 Bangumi Access Token');
    await setToken(bangumiToken.trim());
    setBangumiToken('');
    await loadConfig();
    report('Bangumi 已登录');
  });

  const logoutBangumi = () => runAction('Bangumi 退出', async () => {
    await clearToken();
    await loadConfig();
    report('Bangumi 已退出');
  });

  const testConfig = (kind: 'mpv' | 'tmdb') => runAction('测试连接', async () => {
    const result = kind === 'mpv' ? await configApi.testMpv() : await configApi.testTmdb();
    report(result.message);
  });

  const testMediaPaths = () => runAction('验证媒体路径', async () => {
    const result = await configApi.testMediaPaths();
    setMediaPathValidation(result);
    report(result.ok ? '网盘路径验证通过' : '发现不可用或映射不匹配的网盘路径');
  });

  const updateOpenlistDraft = (key: keyof OpenListDraft, value: string) => {
    setOpenlistDraft((current) => ({ ...current, [key]: value }));
  };

  const updateRouteDraft = (prefix: string, patch: Partial<OpenListRouteItem>) => {
    setRouteDraft((current) => current.map((item) => (item.remote_prefix === prefix ? { ...item, ...patch } : item)));
  };

  const openlistLocalPath = (prefix: string) =>
    openlistRoutes.find((route) => route.remote_prefix === prefix)?.local_path ?? '';

  const renderAppearance = () => (
    <PanelStack>
      <SectionIntro title="外观" />
      <SettingsSection title="应用主题">
        <div className="appearance-grid">
          {([
            { id: 'fluent', name: '雾蓝云母', colors: ['#edf5fb', '#ffffff', '#6c91b0'] },
            { id: 'cinema', name: '深邃影院', colors: ['#090b0f', '#151920', '#a9c1e1'] },
            { id: 'mica', name: '纯白', colors: ['#ffffff', '#ffffff', '#586570'] },
          ] as Array<{ id: AppearanceMode; name: string; colors: string[] }>).map((theme) => (
            <button key={theme.id} className={`appearance-card ${appearanceMode === theme.id ? 'active' : ''}`} onClick={() => setAppearanceMode(theme.id)}>
              <span className="appearance-preview">
                {theme.colors.map((color, index) => <i key={`${theme.id}-${index}`} style={{ background: color }} />)}
              </span>
              <strong>{theme.name}</strong>
              <span className="appearance-state">{appearanceMode === theme.id ? '正在使用' : '切换主题'}</span>
            </button>
          ))}
        </div>
      </SettingsSection>
    </PanelStack>
  );

  const renderScrape = () => (
    <PanelStack>
      <SectionIntro title="元数据与图片" />
      {config && (
        <SettingsSection title="常用连接配置">
          <div className="settings-field-list">
            <div className="settings-credential-guide">
              <div>
                <strong>TMDB 凭据</strong>
                <span>需要 API 设置页上方较长的“API 读取访问令牌”（Bearer Token），不是 API 密钥。</span>
              </div>
              <a href={TMDB_API_SETTINGS_URL} target="_blank" rel="noreferrer">创建或查看令牌 <ExternalLink size={14} /></a>
            </div>
            <ConfigRow label="API 读取访问令牌" value={config.tmdb_bearer_token} secret placeholder="粘贴 TMDB API 读取访问令牌" onSave={saveTmdbCredential} />
          </div>
          <div className="settings-actions">
            <GhostButton onClick={() => testConfig('tmdb')}>测试 TMDB 连接</GhostButton>
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="网络访问">
          <div className="settings-field-list">
            <ConfigRow label="网络代理" value={config.proxy_url} onSave={(value) => saveConfig({ proxy_url: value })} />
            <span className="field-help">代理只用于访问 TMDB、AniList 等外部服务；OpenList 的局域网连接在「WebDAV 设置」中单独管理。</span>
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="图片策略">
          <div className="settings-field-list">
            <SelectRow
              label="图片策略"
              value={config.artwork_storage_mode || 'local'}
              options={[
                { value: 'local', label: '优先使用本地图片' },
                { value: 'auto', label: '本地优先，缺失时联网' },
                { value: 'remote', label: '优先使用远程图片' },
              ]}
              onSave={(value) => saveConfig({ artwork_storage_mode: value as PublicConfig['artwork_storage_mode'] })}
            />
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="联网名称核对（可选）" collapsible>
          <div className="settings-field-list">
            <span className="field-help">仅在常规刮削无法安全确认名称时使用。只发送作品名称与年份进行搜索，并将相关公开网页内容交给 DeepSeek 提取名称；不发送文件路径或网盘账户，结果仍由 TMDB 核对。</span>
            <SelectRow label="联网名称核对" value={config.alias_web_recovery_enabled ? 'on' : 'off'}
              options={[{ value: 'off', label: '关闭（默认）' }, { value: 'on', label: '启用' }]}
              onSave={(value) => saveConfig({ alias_web_recovery_enabled: value === 'on' })} />
            <ConfigRow label="Tavily 搜索密钥" secret value={config.websearch_configured ? 'configured' : ''}
              placeholder="粘贴应用使用的 Tavily API Key" onSave={(value) => saveConfig({ websearch_api_key: value.trim() })} />
            <ConfigRow label="DeepSeek 密钥" secret value={config.deepseek_configured || config.deepseek_api_key ? 'configured' : ''}
              placeholder="粘贴 DeepSeek API Key" onSave={(value) => saveConfig({ deepseek_api_key: value.trim() })} />
            <span className="field-help">需同时保存两个密钥并启用开关；密钥保存在本机安全凭据存储中。</span>
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="高级参数" collapsible>
          <div className="settings-field-list">
            <ConfigRow label="TMDB 语言" value={config.tmdb_language} onSave={(value) => saveConfig({ tmdb_language: value })} />
            <ConfigRow
              label="分级地区优先级"
              value={config.tmdb_certification_regions}
              onSave={(value) => saveConfig({ tmdb_certification_regions: value.toUpperCase().replace(/\s+/g, '') })}
            />
            <NumberRow label="TMDB 超时" value={config.tmdb_timeout} onSave={(value) => saveConfig({ tmdb_timeout: value })} />
            <NumberRow label="TMDB 重试" value={config.tmdb_max_retries} onSave={(value) => saveConfig({ tmdb_max_retries: value })} />
            <ToggleRow label="启用 AniList 辅助" active={config.anilist_enabled} onChange={() => saveConfig({ anilist_enabled: !config.anilist_enabled })} />
            <NumberRow label="AniList 超时" value={config.anilist_timeout} onSave={(value) => saveConfig({ anilist_timeout: value })} />
            <NumberRow label="AniList 请求间隔" value={config.anilist_rate_limit} onSave={(value) => saveConfig({ anilist_rate_limit: value })} />
          </div>
        </SettingsSection>
      )}
    </PanelStack>
  );

  const renderOpenList = () => (
    <PanelStack>
      <SectionIntro title="WebDAV 设置" />
      <p className="sources-root-note">目前仅针对 OpenList 做了适配与优化，目录浏览、来源识别及增量检查依赖其专用接口，尚不支持直接连接其他 WebDAV 服务。通过 CloudDrive2 等工具挂载的目录，可在「媒体来源」中配置并使用本地目录或目录树导入。</p>
      {baseConfig && <div className="settings-connection-toolbar">
        <OpenListConnectionPicker connections={openlistConnections(baseConfig)} value={selectedConnectionId} disabled={Boolean(activeAction || connectionBusy)} onChange={(identity) => {
          setSelectedConnectionId(identity);
          setOpenlistNotice('');
          setRouteNotice('');
          setOpenlistRoutes([]);
          setRouteDraft([]);
          setRouteDiscoverItems([]);
          setRoutesLoaded(false);
        }} />
        <Button appearance="secondary" disabled={Boolean(activeAction || connectionBusy)} onClick={() => void (async () => {
          setConnectionBusy('add');
          try {
            const result = await openlistApi.createConnection('新连接');
            await loadConfig();
            setSelectedConnectionId(result.connection_id);
            setOpenlistNotice('请填写新连接的地址、挂载位置和账号。');
            setOpenlistNoticeKind('info');
          } catch (error) { setOpenlistNotice((error as Error).message); setOpenlistNoticeKind('error'); }
          finally { setConnectionBusy(''); }
        })()}>添加连接</Button>
      </div>}
      {config && (
        <SettingsSection title="连接服务">
          <OpenListSettingsPanel
            key={selectedConnectionId}
            config={config}
            draft={openlistDraft}
            onChangeDraft={updateOpenlistDraft}
            onSaveConnection={async (payload, skipVerification) => {
              setConnectionBusy('save');
              try {
              const result = await openlistApi.saveConfig({
                ...payload,
                skip_verification: skipVerification ?? false,
              });
              if (!result.ok) throw new Error(result.message);
              setOpenlistNoticeKind('success');
              setOpenlistNotice(result.message);
              await loadConfig();
              setOpenlistDraft((current) => ({ ...current, username: '', password: '' }));
              report(result.message);
              // 由面板决定是否显示"连接正常"：只有后端确实探测成功才算
              return { verified: result.verified === true };
              } finally { setConnectionBusy(''); }
            }}
            onTestConnection={async (payload) => {
              setConnectionBusy('test');
              try {
              // REWORK P0：allow_insecure_http 由面板风险确认状态决定，
              // 不在父级 hardcode true；返回后端 machine status code
              const result = await openlistApi.testConnection(payload);
              setOpenlistNoticeKind(result.ok ? 'success' : 'error');
              setOpenlistNotice(result.message);
              return result;
              } finally { setConnectionBusy(''); }
            }}
            notice={openlistNotice}
            noticeKind={openlistNoticeKind}
            onNotice={(message, kind) => {
              setOpenlistNoticeKind(kind);
              setOpenlistNotice(message);
            }}
            externalBusy={activeAction}
          />
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="选择来源目录">
          <OpenListSourceRoutes
            key={selectedConnectionId}
            configured={config.openlist_configured}
            routes={openlistRoutes}
            draft={routeDraft}
            discoverItems={routeDiscoverItems}
            notice={routeNotice}
            busy={activeAction}
            onDiscover={async () => { setConnectionBusy('discover'); try { await discoverRoutes(false); } finally { setConnectionBusy(''); } }}
            onSave={async () => {
              setConnectionBusy('routes');
              try {
              const result = await openlistApi.saveRoutes(routeDraft, selectedConnectionId);
              setOpenlistRoutes(result.routes);
              // 回填已保存路由（含后端生成的 route_id）：
              // 新发现目录保存后不再是「未保存更改」，重复保存也不会反复生成新 route_id。
              setRouteDraft(result.routes.map((route) => ({
                route_id: route.route_id,
                label: route.label,
                remote_prefix: route.remote_prefix,
                provider_id: route.provider_id,
                enabled: route.enabled,
              })));
              setRouteNotice('来源目录已保存。可前往媒体管理选择目录并导入。');
              report('来源目录已保存');
              } finally { setConnectionBusy(''); }
            }}
            onUpdateDraft={updateRouteDraft}
          />
        </SettingsSection>
      )}
    </PanelStack>
  );

  const renderSources = () => (
    <PanelStack>
      <SectionIntro title="媒体来源" />
      {config && <SettingsSection title="新番自动更新">
        <OngoingUpdateSettingsPanel config={config} categoryName={ongoingCategoryName} onSaveCategoryName={setOngoingCategoryName} externalBusy={Boolean(configLoading || activeAction)} onSave={async patch => {
          const updated = await configApi.patchConfig(patch);
          setConfig(updated);
        }} />
      </SettingsSection>}
      {config && (
        <SettingsSection title="来源根目录">
          <div className="sources-root-panel">
            <p className="sources-root-note">选择各网盘在资源管理器中的挂载文件夹，可分别位于 J、K 等不同盘符。目录树导入默认使用这里的路径，也可以为本次导入另选挂载目录。只需配置你使用的来源。</p>
            <div className="settings-field-list">
              <ConfigRow folder label="115 挂载根路径" value={config.pan115_root} onSave={(value) => saveConfig({ pan115_root: value })} />
              <ConfigRow folder label="百度网盘挂载位置" value={config.baidu_root} onSave={(value) => saveConfig({ baidu_root: value })} />
              <ConfigRow folder label="夸克网盘挂载位置" value={config.quark_root || ''} onSave={(value) => saveConfig({ quark_root: value })} />
              <ConfigRow folder label="本地媒体根路径" value={config.local_root} onSave={(value) => saveConfig({ local_root: value })} />
              <ConfigRow folder label="目录树文件目录" value={config.directory_tree_dir} onSave={(value) => saveConfig({ directory_tree_dir: value })} />
            </div>
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="镜像与路径">
          <div className="settings-field-list">
            <ConfigRow folder label="镜像目录" value={config.mirror_dir} onSave={(value) => saveConfig({ mirror_dir: value })} />
          </div>
          <div className="settings-actions">
            <GhostButton onClick={testMediaPaths} busy={activeAction === '验证媒体路径'}>验证媒体路径</GhostButton>
            <span className="field-help">镜像保存播放入口和作品资料，不会移动原始视频。</span>
          </div>
          {mediaPathValidation && (
            <div className="media-path-validation" role="status" aria-label="媒体路径验证结果">
              {mediaPathValidation.sources.map((item) => (
                <div key={item.source} className={`media-path-validation-item ${item.ok ? 'is-ok' : 'is-error'}`}>
                  <div>
                    <strong>{sourceLabels[item.source]}</strong>
                    <span>{item.ok ? '验证通过' : '需要处理'}</span>
                  </div>
                  <p>{item.message}</p>
                  {item.resolved_root && <code>{item.resolved_root}</code>}
                </div>
              ))}
            </div>
          )}
        </SettingsSection>
      )}
      <SettingsSection title="重新配置基础来源">
        <div className="settings-support-row">
          <div>
            <strong>重新检查镜像与媒体根目录</strong>
            <p>使用当前配置重新进入引导并逐项验证。不会清空现有配置，中途退出也不会影响当前媒体库。</p>
          </div>
          <GhostButton onClick={() => onOpenSetup?.()} disabled={!onOpenSetup}>
            <RotateCcw size={16} aria-hidden="true" />重新进入初始引导
          </GhostButton>
        </div>
      </SettingsSection>
    </PanelStack>
  );


  const renderPlayer = () => (
    <PanelStack>
      <SectionIntro title="播放" />
      {config && <PlayerTuningPage embedded initialConfig={config} runtimeStatus={mpvRuntime} externalBusy={Boolean(activeAction || configLoading)} onConfigSaved={setConfig} onPlayerTested={loadMpvRuntime} />}
      {config && <SettingsSection title="连续播放">
        <ToggleRow label="自动播放下一集" active={config.auto_play_next_episode} onChange={() => saveConfig({ auto_play_next_episode: !config.auto_play_next_episode })} />
      </SettingsSection>}
      {config && (
        <SettingsSection title="界面连接监测">
          <div className="settings-field-list">
            <p className="field-help">界面定期向后台服务发送连接信号（心跳），用于发现界面关闭或连接中断，不用于检测视频是否正在播放。</p>
            <ToggleRow label="界面连接监测（心跳）" active={config.heartbeat_enabled} onChange={() => saveConfig({ heartbeat_enabled: !config.heartbeat_enabled })} />
            <NumberRow label="连接超时等待（秒）" value={config.heartbeat_timeout} onSave={(value) => saveConfig({ heartbeat_timeout: value })} />
            <ToggleRow label="连接超时后退出空闲后台" active={config.auto_shutdown_on_heartbeat_timeout} onChange={() => saveConfig({ auto_shutdown_on_heartbeat_timeout: !config.auto_shutdown_on_heartbeat_timeout })} />
            <p className="field-help">启用自动退出后，仅在应用窗口已关闭、连接超过等待时间，并且没有播放、导入等后台任务时结束后台服务；窗口仍打开或任务仍在运行时不会退出。</p>
          </div>
        </SettingsSection>
      )}
    </PanelStack>
  );

  const renderBangumi = () => {
    // 仅当凭据有效且在线才允许显示“已连接”；cached user 只承担身份展示
    const isConnected = hasStoredCredential && authStatus === 'valid' && connectivity === 'online';
    return (
    <PanelStack>
      <SectionIntro title="账户与同步" />
      <SettingsSection className="settings-account-card">
        {user ? (
          <div className={`settings-account-hero${isConnected ? ' is-connected' : ''}`}>
            <div className="settings-account-identity">
              {user.avatar ? <DecodedImage src={buildBangumiImageUrl(user.avatar)} alt={user.nickname || user.username} /> : <span className="settings-account-avatar"><UserRound size={24} /></span>}
              <div className="min-w-0">
                <span className="settings-account-kicker">{isConnected ? 'Bangumi 已连接' : 'Bangumi 账户'}</span>
                <strong>{user.nickname || user.username}</strong>
                <small>@{user.username}{lastSuccessAt ? ` · 上次成功连接 ${lastSuccessAt.slice(0, 16).replace('T', ' ')}` : ''}</small>
              </div>
            </div>
            <GhostButton onClick={logoutBangumi}>退出</GhostButton>
          </div>
        ) : bangumiSessionStatus === 'checking' ? (
          <div className="settings-note settings-bangumi-session"><Spinner size="tiny" />正在恢复已保存的 Bangumi 登录信息…</div>
        ) : hasStoredCredential ? (
          <div className="settings-account-hero">
            <div className="settings-account-identity">
              <span className="settings-account-avatar"><UserRound size={24} /></span>
              <div>
                <span className="settings-account-kicker">登录信息已保存</span>
                <strong>Bangumi 账户</strong>
                <small>登录信息已保存，正在确认连接状态</small>
              </div>
            </div>
          </div>
        ) : (
          <div className="settings-account-hero">
            <div className="settings-account-identity">
              <span className="settings-account-avatar"><UserRound size={24} /></span>
              <div>
                <span className="settings-account-kicker">推荐方式</span>
                <strong>连接 Bangumi 账户</strong>
                <small>在 Bangumi 官方页面创建“个人访问令牌（Access Token）”，再返回 KumiPlayer 完成登录。</small>
              </div>
            </div>
            <a className="settings-official-login" href={BANGUMI_ACCESS_TOKEN_URL} target="_blank" rel="noreferrer">
              创建 Bangumi 个人访问令牌 <ExternalLink size={15} />
            </a>
          </div>
        )}
        {hasStoredCredential && (
          <div className="settings-bangumi-session settings-note">
            {/* 左侧：状态文案（+ 上次成功连接）；右侧：重新验证按钮。
                容器的 CSS 已是 flex + space-between，因此按钮作为第二个子元素即落在右边。 */}
            <div>
              {isConnected ? (
                // 已连接时**不能**再说"尚未验证"，也不能提示去点一个不存在的按钮
                // （用户实测：顶栏写着"BANGUMI 已连接"，下面却写"尚未验证连接"且右侧没有按钮）。
                <div><strong>Bangumi 连接正常</strong><span>收藏状态与已看集数会自动同步。</span></div>
              ) : authStatus === 'reauth_required' ? (
                <div><strong>Bangumi 登录授权已失效</strong><span>账户资料仍保存在本机，请更新 Personal Access Token。</span></div>
              ) : connectivity === 'rate_limited' ? (
                <div><strong>Bangumi 请求暂时受限</strong><span>登录信息仍然有效，稍后会自动恢复。</span></div>
              ) : connectivity === 'forbidden' ? (
                <div><strong>Bangumi 拒绝了本次请求</strong><span>登录信息仍然保存着，请稍后重新验证。</span></div>
              ) : connectivity === 'offline' || connectivity === 'server_error' ? (
                <div><strong>登录信息已保存，暂时无法连接 Bangumi</strong><span>本地服务、代理或网络恢复后会自动恢复。</span></div>
              ) : credentialState === 'unavailable' ? (
                <div><strong>暂时无法读取本机 Bangumi 登录凭据</strong><span>请检查 Windows Credential Manager 是否可用；已保存的账户资料不会被清除。</span></div>
              ) : bangumiLoading ? (
                <div><strong>正在验证 Bangumi 登录状态</strong><span>稍等一下；若长时间停在这里，点右侧按钮重试。</span></div>
              ) : (
                <div><strong>登录信息已保存</strong><span>点右侧「重新验证」确认当前连接。</span></div>
              )}
              {lastSuccessAt && !isConnected && <small>上次成功连接：{lastSuccessAt.slice(0, 16).replace('T', ' ')}</small>}
            </div>
            {/* 按钮始终在右侧（容器是 flex + space-between）：已连接时它是"重新检查"，
                其余状态是"重新验证"。绝不再出现"提示去点右侧按钮但右侧没有按钮"。 */}
            <GhostButton onClick={() => void verifyBangumiSession()} disabled={bangumiLoading}>
              {bangumiLoading ? '正在验证…' : isConnected ? '重新检查' : '重新验证'}
            </GhostButton>
          </div>
        )}
        {authStatus === 'reauth_required' && bangumiError && <div className="settings-note danger">已保存的登录信息无法通过验证：{bangumiError}</div>}
      </SettingsSection>

      {(!hasStoredCredential || authStatus === 'reauth_required') && bangumiSessionStatus !== 'checking' && (
        <details className="settings-section settings-token-login" open={authStatus === 'reauth_required'}>
          <summary className="settings-section-head">
            <span className="settings-token-title"><KeyRound size={17} /><span><strong>个人访问令牌登录</strong><small>Bangumi Access Token</small></span></span>
            <span className="settings-collapse-hint">展开</span>
          </summary>
          <div className="settings-collapsible-body">
            <div className="settings-inline">
              <input type="password" value={bangumiToken} onChange={(event) => setBangumiToken(event.target.value)} className="settings-input min-w-0 flex-1" placeholder="粘贴 Bangumi 个人访问令牌" autoComplete="off" />
              <PrimaryButton onClick={loginBangumi} busy={bangumiLoading} disabled={!bangumiToken.trim()}>{authStatus === 'reauth_required' ? '更新登录信息' : '验证并登录'}</PrimaryButton>
            </div>
            <div className="settings-note">令牌只保存在本机安全存储中，提交前会先通过 Bangumi 当前用户接口验证；授权失效时更新 Token 不会丢失已保存的账户资料。</div>
          </div>
        </details>
      )}

    </PanelStack>
    );
  };

  const contentByTab: Record<SettingsTab, ReactNode> = {
    appearance: renderAppearance(),
    sources: renderSources(),
    openlist: renderOpenList(),
    scrape: renderScrape(),
    player: renderPlayer(),
    bangumi: renderBangumi(),
  };

  return (
    <div className="settings-shell settings-shell-settings settings-focused">
      <main className="settings-content fade-in-soft">
        {(configLoading || activeAction) && (
          <div className="settings-loading">
            <Spinner size="tiny" />
            {activeAction ? `${activeAction}中...` : '保存或加载中...'}
          </div>
        )}
        {operationMessage && !activeAction && (
          <div className="settings-operation-status" role="status">{operationMessage}</div>
        )}
        <div className="settings-page-stack settings-unified-stack">
          {sectionTabs.map((tab) => (
            <section
              key={tab.key}
              id={sectionId(tab.key)}
              hidden={activeSection !== tab.key}
              aria-labelledby={`settings-tab-${tab.key}`}
              className="settings-scroll-section settings-anchor-section"
            >
              {contentByTab[tab.key]}
            </section>
          ))}
        </div>
      </main>
      <aside className="settings-outline-popover-wrap">
        <nav className="settings-outline" aria-label="设置分类">
          <div className="settings-outline-heading">
            <strong>设置</strong>
          </div>
          <div className="settings-outline-nav">
            {sectionTabs.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.key}
                  id={`settings-tab-${tab.key}`}
                  aria-controls={sectionId(tab.key)}
                  type="button"
                  onClick={() => selectSection(tab.key)}
                  className={`settings-outline-link ${activeSection === tab.key ? 'is-active' : ''}`}
                  aria-current={activeSection === tab.key ? 'location' : undefined}
                >
                  <span className="settings-outline-icon"><Icon size={17} strokeWidth={1.8} /></span>
                  <span className="settings-outline-copy"><strong>{tab.label}</strong></span>
                </button>
              );
            })}
          </div>
        </nav>
      </aside>
    </div>
  );
}

function PanelStack({ children }: { children: ReactNode }) {
  return <div className="settings-panel-stack">{children}</div>;
}

function SectionIntro({ title, description = '' }: { title: string; description?: string }) {
  return (
    <div className="settings-intro">
      <h2>{title}</h2>
      {description && <p>{description}</p>}
    </div>
  );
}

function SettingsSection({ title, action, children, collapsible = false, className = '' }: { title?: string; action?: ReactNode; children: ReactNode; collapsible?: boolean; className?: string }) {
  const cls = className ? ` ${className}` : '';
  if (collapsible) {
    return (
      <details className={`settings-section settings-collapsible${cls}`}>
        <summary className="settings-section-head">
          <h3>{title}</h3>
          <span className="settings-collapse-hint">展开</span>
        </summary>
        <div className="settings-collapsible-body">{children}</div>
      </details>
    );
  }
  return (
    <section className={`settings-section${cls}`}>
      {title != null && (
        <div className="settings-section-head">
          <h3>{title}</h3>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

function ConfigRow({ label, value, onSave, secret = false, placeholder = '', folder = false }: { label: string; value: string; onSave: (value: string) => void; secret?: boolean; placeholder?: string; folder?: boolean }) {
  const inputId = useId();
  const initialDraft = secret ? '' : value || '';
  const [draft, setDraft] = useState(initialDraft);
  useEffect(() => setDraft(secret ? '' : value || ''), [secret, value]);
  const resolvedPlaceholder = secret && value ? '已配置；粘贴新凭据可替换' : placeholder;
  return (
    <div className="settings-config-row">
      <label htmlFor={inputId}>{label}</label>
      {folder ? <FolderPathInput inputId={inputId} label={label} value={draft} onChange={setDraft} placeholder={placeholder} /> : <input id={inputId} type={secret ? 'password' : 'text'} value={draft} onChange={(event) => setDraft(event.target.value)} className="settings-input" placeholder={resolvedPlaceholder} autoComplete={secret ? 'off' : undefined} />}
      <GhostButton onClick={() => { onSave(draft); if (secret) setDraft(''); }} disabled={secret && !draft.trim()}>保存</GhostButton>
    </div>
  );
}

function NumberRow({ label, value, onSave }: { label: string; value: number; onSave: (value: number) => void }) {
  const [draft, setDraft] = useState(String(value ?? 0));
  useEffect(() => setDraft(String(value ?? 0)), [value]);
  return (
    <div className="settings-config-row">
      <label>{label}</label>
      <input type="number" value={draft} onChange={(event) => setDraft(event.target.value)} className="settings-input" />
      <GhostButton onClick={() => onSave(Number(draft) || 0)}>保存</GhostButton>
    </div>
  );
}

function SelectRow({ label, value, options, onSave }: { label: string; value: string; options: Array<{ value: string; label: string }>; onSave: (value: string) => void }) {
  const inputId = useId();
  const [draft, setDraft] = useState(value || '');
  useEffect(() => setDraft(value || ''), [value]);
  return (
    <div className="settings-config-row">
      <label htmlFor={inputId}>{label}</label>
      <select id={inputId} value={draft} onChange={(event) => setDraft(event.target.value)} className="settings-input">
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <GhostButton onClick={() => onSave(draft)}>保存</GhostButton>
    </div>
  );
}

function ToggleRow({ label, active, onChange }: { label: string; active: boolean; onChange: () => void }) {
  return (
    <div className="settings-toggle-row">
      <span>{label}</span>
      <button role="switch" aria-checked={Boolean(active)} onClick={onChange} className={`settings-switch ${active ? 'on' : ''}`} aria-label={label}>
        <span />
      </button>
    </div>
  );
}

function PrimaryButton({ onClick, children, busy = false, disabled = false }: { onClick: () => void; children: ReactNode; busy?: boolean; disabled?: boolean }) {
  return (
    <Button appearance="primary" onClick={onClick} className="settings-primary-btn fluent-settings-btn" disabled={disabled || busy} icon={busy ? <Spinner size="tiny" /> : undefined}>
      {busy ? '处理中' : children}
    </Button>
  );
}

function GhostButton({ onClick, children, busy = false, disabled = false }: { onClick: () => void; children: ReactNode; busy?: boolean; disabled?: boolean }) {
  return (
    <Button appearance="secondary" onClick={onClick} className="settings-ghost-btn fluent-settings-btn" disabled={disabled || busy} icon={busy ? <Spinner size="tiny" /> : undefined}>
      {busy ? '处理中' : children}
    </Button>
  );
}

function EmptyText({ children }: { children: ReactNode }) {
  return <p className="settings-empty">{children}</p>;
}

function consumeLibraryRefreshMarker(task: TaskRecord, seen: Set<string>) {
  const result = (task.result || {}) as { library_refreshed?: unknown };
  const marker = typeof result.library_refreshed === 'string' ? result.library_refreshed : '';
  if (!marker) return false;
  const key = `${task.task_id}:${marker}`;
  if (seen.has(key)) return false;
  seen.add(key);
  return true;
}

function formatShortDate(value: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
