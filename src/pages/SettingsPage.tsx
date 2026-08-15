import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Button, Spinner } from '@fluentui/react-components';
import {
  Database,
  Download,
  ExternalLink,
  HeartHandshake,
  KeyRound,
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
import { scrapeApi } from '../api/scrape';
import { tasksApi } from '../api/tasks';
import { exportErrorLogText } from '../api/errorLog';
import type { TaskRecord } from '../api/types';
import { useUiStore, type AppearanceMode } from '../stores/ui';
import { BANGUMI_ACCESS_TOKEN_URL, getTmdbCredentialError, TMDB_API_SETTINGS_URL } from '../config/credentials';
import DecodedImage from '../components/ui/DecodedImage';

type SettingsTab = 'appearance' | 'scrape' | 'player' | 'bangumi' | 'support';
type SourceKey = 'pan115' | 'baidu' | 'local';
type OpenListDraft = Pick<OpenListConfigPayload, 'server_url' | 'remote_root' | 'mount_root' | 'username' | 'password'> & {
  cache_ttl: string;
  prefetch_limit: string;
};

const sectionTabs: Array<{ key: SettingsTab; label: string; summary: string; icon: LucideIcon }> = [
  { key: 'bangumi', label: '账户与同步', summary: 'Bangumi 登录与观看同步', icon: UserRound },
  { key: 'appearance', label: '外观', summary: '主题、卡片与显示密度', icon: Palette },
  { key: 'scrape', label: '媒体与元数据', summary: 'TMDB、AniList 与刮削', icon: Database },
  { key: 'player', label: '播放', summary: 'mpv 与连续播放', icon: PlaySquare },
  { key: 'support', label: '应用与支持', summary: '初始引导与赞助入口', icon: HeartHandshake },
];

const sourceLabels: Record<SourceKey | 'all' | 'openlist', string> = {
  all: '全部来源',
  pan115: '115 网盘',
  baidu: '百度网盘',
  openlist: 'OpenList 连接',
  local: '本地',
};

// OpenList 提供商选项（不含 local：local 仅用于本地来源）
const routeProviderOptions: Array<{ value: ProviderId; label: string }> = [
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'quark', label: '夸克网盘' },
  { value: 'other', label: '其他远程来源' },
];

const sectionId = (key: SettingsTab) => `settings-panel-${key}`;

function buildOpenListWebdavAddress(serverUrl: string) {
  const base = serverUrl.trim().replace(/\/+$/, '');
  if (!base) return '';
  const apiBase = base.toLowerCase().endsWith('/dav') ? base.slice(0, -4) : base;
  return `${apiBase}/dav/`;
}

// 非回环 http 才需要明文风险确认；localhost / 127.x / ::1 直接放行
function isNonLoopbackHttp(url: string): boolean {
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
  const { appearanceMode, setAppearanceMode, goPlayerTuning } = useUiStore();
  const [activeSection, setActiveSection] = useState<SettingsTab>('bangumi');
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [mpvRuntime, setMpvRuntime] = useState<MpvRuntimeStatus | null>(null);
  const [operationMessage, setOperationMessage] = useState('');
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [mediaPathValidation, setMediaPathValidation] = useState<MediaPathValidationResponse | null>(null);
  const [activeSource, setActiveSource_] = useState<SourceKey>(
    () => (localStorage.getItem('kumiplayer-active-source') as SourceKey) || 'pan115'
  );
  const setActiveSource = (val: SourceKey) => {
    setActiveSource_(val);
    localStorage.setItem('kumiplayer-active-source', val);
  };
  const [bangumiToken, setBangumiToken] = useState('');
  const [openlistNotice, setOpenlistNotice] = useState('');
  const [openlistNoticeKind, setOpenlistNoticeKind] = useState<'success' | 'error' | 'info'>('info');
  const [allowOpenlistHttp, setAllowOpenlistHttp] = useState(false);
  const [openlistDraft, setOpenlistDraft] = useState<OpenListDraft>({
    server_url: '', remote_root: '/', mount_root: '', username: 'admin', password: '',
    cache_ttl: '1440', prefetch_limit: '12',
  });
  const [openlistRoutes, setOpenlistRoutes] = useState<OpenListRoute[]>([]);
  const [routeDraft, setRouteDraft] = useState<OpenListRouteItem[]>([]);
  const [routeDiscoverItems, setRouteDiscoverItems] = useState<OpenListDiscoverItem[]>([]);
  const [routeNotice, setRouteNotice] = useState('');
  const seenLibraryRefreshRef = useRef<Set<string>>(new Set());
  const navigationTargetRef = useRef<SettingsTab | null>(null);
  const navigationUnlockTimerRef = useRef<number | null>(null);

  useEffect(() => {
    void loadCore();
  }, []);

  useEffect(() => {
    if (activeSection !== 'scrape') return;
    const timer = window.setInterval(() => {
      void refreshTasks(12).catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [activeSection, activeSource]);

  useEffect(() => {
    if (!config) return;
    setOpenlistDraft({
      server_url: config.openlist_server_url,
      remote_root: config.openlist_remote_root || '/',
      mount_root: config.openlist_mount_root,
      username: config.openlist_configured ? '' : 'admin',
      password: '',
      cache_ttl: String(config.openlist_cache_ttl_minutes ?? 1440),
      prefetch_limit: String(config.openlist_prefetch_limit ?? 12),
    });
  }, [config?.openlist_server_url, config?.openlist_remote_root, config?.openlist_mount_root, config?.openlist_configured, config?.openlist_cache_ttl_minutes, config?.openlist_prefetch_limit]);

  // 提供商路由：读取已保存路由
  useEffect(() => {
    if (!config?.openlist_configured) return;
    void openlistApi.getRoutes()
      .then((result) => {
        setOpenlistRoutes(result.routes);
        setRouteDraft(result.routes.map((route) => ({
          route_id: route.route_id,
          label: route.label,
          remote_prefix: route.remote_prefix,
          provider_id: route.provider_id,
          enabled: route.enabled,
        })));
      })
      .catch(() => setRouteNotice('读取来源路由失败，请确认 OpenList 连接配置'));
  }, [config?.openlist_configured, config?.openlist_server_url]);


  useEffect(() => {
    const sections = sectionTabs
      .map((tab) => document.getElementById(sectionId(tab.key)))
      .filter((section): section is HTMLElement => Boolean(section));
    if (!sections.length) return undefined;

    const observer = new IntersectionObserver((entries) => {
      if (navigationTargetRef.current) return;
      const current = entries
        .filter((entry) => entry.isIntersecting)
        .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
      if (!current) return;
      const matched = sectionTabs.find((tab) => sectionId(tab.key) === current.target.id);
      if (matched) setActiveSection(matched.key);
    }, { rootMargin: '-116px 0px -58% 0px', threshold: [0.08, 0.35, 0.65] });

    sections.forEach((section) => observer.observe(section));
    return () => {
      observer.disconnect();
      if (navigationUnlockTimerRef.current != null) {
        window.clearTimeout(navigationUnlockTimerRef.current);
      }
    };
  }, []);

  const selectSection = (key: SettingsTab) => {
    navigationTargetRef.current = key;
    if (navigationUnlockTimerRef.current != null) {
      window.clearTimeout(navigationUnlockTimerRef.current);
    }
    setActiveSection(key);
    const target = document.getElementById(sectionId(key));
    if (!target) {
      navigationTargetRef.current = null;
      return;
    }

    const root = document.documentElement;
    const body = document.body;
    const previousRootBehavior = root.style.scrollBehavior;
    const previousBodyBehavior = body.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    body.style.scrollBehavior = 'auto';
    try {
      target.scrollIntoView({ behavior: 'auto', block: 'start' });
    } finally {
      root.style.scrollBehavior = previousRootBehavior;
      body.style.scrollBehavior = previousBodyBehavior;
    }

    navigationUnlockTimerRef.current = window.setTimeout(() => {
      navigationTargetRef.current = null;
      navigationUnlockTimerRef.current = null;
    }, 120);
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

  const checkMpvRuntime = () => runAction('检测内置播放器', async () => {
    await loadMpvRuntime();
    report('内置播放器状态已刷新');
  });

  const openMpvConfigDir = () => runAction('打开 MPV 配置目录', async () => {
    const result = await configApi.openMpvConfigDir();
    report(`已打开 MPV 配置目录：${result.config_dir}`);
  });

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

  const backfillCertifications = () => runAction('补全缺失分级', async () => {
    const task = await scrapeApi.backfillCertifications();
    await refreshTasks(12);
    report(`分级补全任务已提交：${task.task_id}`);
  });

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

  const saveOpenlistConfig = () => runAction('保存 OpenList 连接', async () => {
    if (!config) throw new Error('配置尚未加载');
    const payload: OpenListConfigPayload = {
      ...openlistDraft,
      allow_insecure_http: allowOpenlistHttp || isNonLoopbackHttp(openlistDraft.server_url) === false,
      cache_ttl_minutes: Math.max(1, Number(openlistDraft.cache_ttl) || 1440),
      prefetch_limit: Math.max(0, Math.min(50, Number(openlistDraft.prefetch_limit) || 12)),
    };
    const result = await openlistApi.saveConfig(payload);
    // 只有后端确认保存成功才提示成功
    if (!result.ok) throw new Error(result.message);
    setOpenlistNoticeKind('success');
    setOpenlistNotice(result.message);
    await loadConfig();
    report(result.message);
  });

  const testOpenlistConnection = () => runAction('测试 OpenList 连接', async () => {
    const result = await openlistApi.testConnection({
      server_url: openlistDraft.server_url,
      username: openlistDraft.username,
      password: openlistDraft.password,
      allow_insecure_http: allowOpenlistHttp || isNonLoopbackHttp(openlistDraft.server_url) === false,
    });
    // 连接成功绿色提示，失败红色提示
    setOpenlistNoticeKind(result.ok ? 'success' : 'error');
    setOpenlistNotice(result.message);
    report(result.message);
  });

  const updateOpenlistDraft = (key: keyof OpenListDraft, value: string) => {
    setOpenlistDraft((current) => ({ ...current, [key]: value }));
  };

  const discoverOpenlistRoutes = () => runAction('读取 OpenList 来源目录', async () => {
    const result = await openlistApi.discoverRoutes();
    setRouteDiscoverItems(result.items);
    // 已保存的路由保留原值；新目录使用名称建议值（仅建议，用户确认后才成为事实）
    const saved = new Map(routeDraft.map((item) => [item.remote_prefix, item]));
    const next: OpenListRouteItem[] = result.items.map((item) => {
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
    setRouteDraft(next);
    setRouteNotice(`已读取 ${result.items.length} 个顶层目录；目录名建议仅供参考，请确认内容提供商后再保存`);
  });

  const saveOpenlistRoutes = () => runAction('保存来源目录路由', async () => {
    const result = await openlistApi.saveRoutes(routeDraft);
    setOpenlistRoutes(result.routes);
    setRouteNotice('来源目录路由已保存；未勾选“可作为媒体来源”的目录仍可浏览，但不能导入');
    report('来源目录路由已保存');
  });

  const updateRouteDraft = (prefix: string, patch: Partial<OpenListRouteItem>) => {
    setRouteDraft((current) => current.map((item) => (item.remote_prefix === prefix ? { ...item, ...patch } : item)));
  };

  const openlistLocalPath = (prefix: string) =>
    openlistRoutes.find((route) => route.remote_prefix === prefix)?.local_path ?? '';

  const renderAppearance = () => (
    <PanelStack>
      <SectionIntro title="外观" description="选择适合观影和管理场景的界面风格，偏好会自动保留。" />
      <SettingsSection title="应用主题">
        <div className="appearance-grid">
          {([
            { id: 'fluent', name: '雾蓝云母', colors: ['#edf5fb', '#ffffff', '#6c91b0'] },
            { id: 'cinema', name: '深邃影院', colors: ['#090b0f', '#151920', '#a9c1e1'] },
            { id: 'mica', name: '纯白', colors: ['#ffffff', '#ffffff', '#586570'] },
          ] as Array<{ id: AppearanceMode; name: string; colors: string[] }>).map((theme) => (
            <button key={theme.id} className={`appearance-card ${appearanceMode === theme.id ? 'active' : ''}`} onClick={() => setAppearanceMode(theme.id)}>
              <span className="appearance-preview">
                {theme.colors.map((color) => <i key={color} style={{ background: color }} />)}
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
      <SectionIntro title="媒体与元数据" description="管理刮削来源、图片保存方式和服务连接。" />
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
            <ConfigRow label="网络代理" value={config.proxy_url} onSave={(value) => saveConfig({ proxy_url: value })} />
          </div>
        </SettingsSection>
      )}
      {config && (
        <SettingsSection title="路径与图片">
          <div className="settings-field-list">
            <ConfigRow label="115 挂载根路径" value={config.pan115_root} onSave={(value) => saveConfig({ pan115_root: value })} />
            <ConfigRow label="百度网盘挂载位置" value={config.baidu_root} onSave={(value) => saveConfig({ baidu_root: value })} />
            <ConfigRow label="本地媒体根路径" value={config.local_root} onSave={(value) => saveConfig({ local_root: value })} />
            <ConfigRow label="目录树文件目录" value={config.directory_tree_dir} onSave={(value) => saveConfig({ directory_tree_dir: value })} />
            <ConfigRow label="镜像目录" value={config.mirror_dir} onSave={(value) => saveConfig({ mirror_dir: value })} />
            <div className="settings-openlist-panel">
              <div className="settings-openlist-head">
                <strong>OpenList 连接</strong>
                <span>OpenList 是取得目录的连接/导入方式，不是内容提供商；115、百度、夸克才是内容提供商。连接只配置一次：服务地址、账号密码、远端总根与本地总挂载根。用户名与密码仅保存在本机凭据管理器，不会回显到界面。OpenList 上游目录缓存的时长需在其存储配置中调整，KumiPlayer 不修改服务端配置；本页的缓存时长只控制 KumiPlayer 本地浏览缓存。</span>
              </div>
              <div className="settings-openlist-status-row">
                <span className={`settings-openlist-status ${config.openlist_configured ? 'ok' : ''}`}>{config.openlist_configured ? '已配置' : '未配置'}</span>
                <small>{config.openlist_configured ? '账号与密码已保存；留空即可继续使用，填写新值后可覆盖。' : '首次保存时请输入账号和密码；凭据仅存入本机凭据管理器。'}</small>
              </div>
              <div className="settings-field-list settings-openlist-form">
                <label className="settings-config-row"><span>OpenList 地址</span><input type="url" value={openlistDraft.server_url} onChange={(event) => updateOpenlistDraft('server_url', event.target.value)} className="settings-input" placeholder="http://localhost:5244" autoComplete="url" /></label>
                <div className="settings-config-row settings-openlist-derived"><span>挂载地址</span><output>{buildOpenListWebdavAddress(openlistDraft.server_url) || '填写 OpenList 地址后自动生成'}</output></div>
                <label className="settings-config-row"><span>远端总根路径</span><input type="text" value={openlistDraft.remote_root} onChange={(event) => updateOpenlistDraft('remote_root', event.target.value)} className="settings-input" placeholder="/" /></label>
                <label className="settings-config-row"><span>本地总挂载根路径</span><input type="text" value={openlistDraft.mount_root} onChange={(event) => updateOpenlistDraft('mount_root', event.target.value)} className="settings-input" placeholder="K:\\" /></label>
                <label className="settings-config-row"><span>浏览缓存时长（分钟）</span><input type="number" min={1} max={43200} value={openlistDraft.cache_ttl} onChange={(event) => updateOpenlistDraft('cache_ttl', event.target.value)} className="settings-input" placeholder="1440" /></label>
                <label className="settings-config-row"><span>预取直接子目录数（上限 50）</span><input type="number" min={0} max={50} value={openlistDraft.prefetch_limit} onChange={(event) => updateOpenlistDraft('prefetch_limit', event.target.value)} className="settings-input" placeholder="12" /></label>
                <label className="settings-config-row"><span>OpenList 用户名</span><input type="text" value={openlistDraft.username} onChange={(event) => updateOpenlistDraft('username', event.target.value)} className="settings-input" placeholder={config.openlist_configured ? '已保存；填写新账号可覆盖' : 'admin'} autoComplete="username" /></label>
                <label className="settings-config-row"><span>OpenList 密码</span><input type="password" value={openlistDraft.password} onChange={(event) => updateOpenlistDraft('password', event.target.value)} className="settings-input" placeholder={config.openlist_configured ? '已保存；填写新密码可覆盖' : '输入 OpenList 密码'} autoComplete="current-password" /></label>
              </div>
              <div className="settings-actions">
                <PrimaryButton onClick={() => void saveOpenlistConfig()} busy={activeAction === '保存 OpenList 连接'}>保存连接</PrimaryButton>
                <GhostButton onClick={() => testOpenlistConnection()} busy={activeAction === '测试 OpenList 连接'}>测试连接</GhostButton>
                {isNonLoopbackHttp(openlistDraft.server_url) && (
                  <label className="settings-risk-confirm">
                    <input type="checkbox" checked={allowOpenlistHttp} onChange={(event) => setAllowOpenlistHttp(event.target.checked)} />
                    该地址是本地/局域网 HTTP，密码将以明文传输；勾选表示已知晓风险
                  </label>
                )}
              </div>
              {openlistNotice && <p className={`settings-openlist-notice${openlistNoticeKind !== 'info' ? ` ${openlistNoticeKind}` : ''}`}>{openlistNotice}</p>}
            </div>
            <div className="settings-openlist-panel settings-routes-panel">
              <div className="settings-openlist-head">
                <strong>来源目录路由</strong>
                <span>为远端总根下的顶层目录指定内容提供商。本地路径由「本地总挂载根 + 远端相对路径」自动推导，无需为每个提供商重复填写挂载路径。目录名建议仅供参考，保存后你的确认值才是事实；未勾选“可作为媒体来源”的目录仍可浏览，但不能导入。</span>
              </div>
              {config.openlist_configured ? (
                <>
                  <div className="settings-actions">
                    <GhostButton onClick={() => void discoverOpenlistRoutes()} busy={activeAction === '读取 OpenList 来源目录'}>读取来源目录</GhostButton>
                    <PrimaryButton onClick={() => void saveOpenlistRoutes()} busy={activeAction === '保存来源目录路由'} disabled={routeDraft.length === 0}>保存路由</PrimaryButton>
                  </div>
                  {routeNotice && <p className="settings-openlist-notice">{routeNotice}</p>}
                  {routeDraft.length > 0 && (
                    <div className="settings-route-list">
                      {routeDraft.map((route) => {
                        const localPath = openlistLocalPath(route.remote_prefix);
                        return (
                          <div key={route.remote_prefix} className="settings-route-row">
                            <label className="settings-route-enabled" title="不作为媒体来源时取消勾选（仍可浏览）">
                              <input type="checkbox" checked={route.enabled} onChange={(event) => updateRouteDraft(route.remote_prefix, { enabled: event.target.checked })} />
                              <span>可作为媒体来源</span>
                            </label>
                            <label className="settings-config-row settings-route-prefix"><span>远端目录</span><input type="text" value={route.remote_prefix} readOnly className="settings-input" /></label>
                            <label className="settings-config-row"><span>来源标签</span><input type="text" value={route.label} onChange={(event) => updateRouteDraft(route.remote_prefix, { label: event.target.value })} className="settings-input" /></label>
                            <label className="settings-config-row"><span>内容提供商</span>
                              <select value={route.provider_id} onChange={(event) => updateRouteDraft(route.remote_prefix, { provider_id: event.target.value as ProviderId })} className="settings-input">
                                {routeProviderOptions.map((option) => (
                                  <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                              </select>
                            </label>
                            {localPath && (
                              <div className="settings-config-row settings-openlist-derived"><span>推导本地路径（只读）</span><output>{localPath}</output></div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              ) : (
                <p className="settings-openlist-notice">请先保存 OpenList 连接，再读取并配置来源目录路由。</p>
              )}
            </div>
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
          <div className="settings-actions">
            <GhostButton onClick={testMediaPaths} busy={activeAction === '验证媒体路径'}>验证媒体路径</GhostButton>
            <span className="field-help">百度目录树会根据文件名自动补齐“01动画”“新番”等目录，并抽样验证真实视频；无需逐次配置。</span>
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
      <SettingsSection title="服务连接" action={<SourceSelect value={activeSource} onChange={setActiveSource} />}>
        <div className="settings-actions">
          <GhostButton onClick={() => testConfig('tmdb')}>测试 TMDB</GhostButton>
        </div>
      </SettingsSection>
      {config && (
        <SettingsSection title="高级参数" collapsible>
          <div className="settings-field-list">
            <ConfigRow label="TMDB 语言" value={config.tmdb_language} onSave={(value) => saveConfig({ tmdb_language: value })} />
            <ConfigRow
              label="分级地区优先级"
              value={config.tmdb_certification_regions}
              onSave={(value) => saveConfig({ tmdb_certification_regions: value.toUpperCase().replace(/\s+/g, '') })}
            />
            <div className="settings-actions">
              <GhostButton onClick={backfillCertifications}>补全缺失分级</GhostButton>
            </div>
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

  const renderPlayer = () => (
    <PanelStack>
      <SectionIntro title="播放" description="内置播放器、连续播放与桌面后台行为。" />
      {config && (
        <SettingsSection title="内置播放器">
          <div className="settings-field-list">
            <div className="mpv-runtime-card">
              <div className="mpv-runtime-head">
                <strong>{mpvRuntime?.version || '内置播放器'}</strong>
                {mpvRuntime?.available
                  ? <span className="mpv-runtime-badge ok">可用</span>
                  : <span className="mpv-runtime-badge error">缺失</span>}
              </div>
              {mpvRuntime?.architecture && (
                <div className="mpv-runtime-meta">{mpvRuntime.architecture}{mpvRuntime.target_triple ? ` · ${mpvRuntime.target_triple}` : ''}{mpvRuntime.distribution_status === 'development-only' ? ' · 本地开发状态' : ''}</div>
              )}
              <div className="mpv-runtime-meta">
                配置：{mpvRuntime?.configuration_available ? '就绪' : '缺失'} · 脚本：{mpvRuntime?.scripts_available ? '就绪' : '缺失'} · 清单：{mpvRuntime?.manifest_valid ? '有效' : '无效'} · 文件：{mpvRuntime?.files_valid ? '校验通过' : '校验失败'}
              </div>
              {mpvRuntime?.message && <div className="mpv-runtime-message">{mpvRuntime.message}</div>}
            </div>
            <ToggleRow label="自动播放下一集" active={config.auto_play_next_episode} onChange={() => saveConfig({ auto_play_next_episode: !config.auto_play_next_episode })} />
            <ToggleRow label="播放心跳" active={config.heartbeat_enabled} onChange={() => saveConfig({ heartbeat_enabled: !config.heartbeat_enabled })} />
            <NumberRow label="心跳超时秒数" value={config.heartbeat_timeout} onSave={(value) => saveConfig({ heartbeat_timeout: value })} />
            <ToggleRow label="心跳超时后自动结束" active={config.auto_shutdown_on_heartbeat_timeout} onChange={() => saveConfig({ auto_shutdown_on_heartbeat_timeout: !config.auto_shutdown_on_heartbeat_timeout })} />
          </div>
          <div className="settings-actions">
            <GhostButton onClick={() => checkMpvRuntime()} disabled={activeAction !== null}>{activeAction === '检测内置播放器' ? '检测中…' : '重新检测内置播放器'}</GhostButton>
            <GhostButton onClick={() => goPlayerTuning()}>播放器调节（Anime4K 默认效果）</GhostButton>
            <GhostButton onClick={() => openMpvConfigDir()} busy={activeAction === '打开 MPV 配置目录'}>打开 MPV 配置目录</GhostButton>
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
      <SectionIntro title="账户与同步" description="连接 Bangumi 后，KumiPlayer 可以同步收藏状态和已看集数。" />
      <SettingsSection className="settings-account-card">
        {user ? (
          <div className={`settings-account-hero${isConnected ? ' is-connected' : ''}`}>
            <div className="settings-account-identity">
              {user.avatar ? <DecodedImage src={buildBangumiImageUrl(user.avatar)} alt={user.nickname || user.username} /> : <span className="settings-account-avatar"><UserRound size={24} /></span>}
              <div className="min-w-0">
                <span className="settings-account-kicker">{isConnected ? 'Bangumi 已连接' : 'Bangumi 账户'}</span>
                <strong>{user.nickname || user.username}</strong>
                <small>@{user.username} · ID {user.id ?? '-'}{lastSuccessAt ? ` · 上次成功连接 ${lastSuccessAt.slice(0, 16).replace('T', ' ')}` : ''}</small>
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
                <small>尚未验证用户资料</small>
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
            {authStatus === 'reauth_required' ? (
              <div><strong>Bangumi 登录授权已失效</strong><span>账户资料仍保存在本机，请更新 Personal Access Token。</span></div>
            ) : connectivity === 'rate_limited' ? (
              <div><strong>Bangumi 请求暂时受限</strong><span>登录信息仍然有效，稍后会自动恢复。</span></div>
            ) : connectivity === 'forbidden' ? (
              <div><strong>Bangumi 拒绝了本次请求</strong><span>登录信息仍然保存着，请稍后重新验证。</span></div>
            ) : connectivity === 'offline' || connectivity === 'server_error' ? (
              <div><strong>登录信息已保存，暂时无法连接 Bangumi</strong><span>本地服务、代理或网络恢复后会自动恢复。</span></div>
            ) : credentialState === 'unavailable' ? (
              <div><strong>暂时无法读取本机 Bangumi 登录凭据</strong><span>请检查 Windows Credential Manager 是否可用；已保存的账户资料不会被清除。</span></div>
            ) : (
              <div><strong>登录信息已保存</strong><span>尚未验证 Bangumi 连接。</span></div>
            )}
            {lastSuccessAt && !isConnected && <small>上次成功连接：{lastSuccessAt.slice(0, 16).replace('T', ' ')}</small>}
            {!isConnected && (
              <GhostButton onClick={() => void verifyBangumiSession()} disabled={bangumiLoading}>重新验证</GhostButton>
            )}
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

  const renderSupport = () => (
    <PanelStack>
      <SectionIntro title="应用与支持" description="重新检查初始环境，或了解后续支持 KumiPlayer 的方式。" />
      <SettingsSection title="初始设置引导">
        <div className="settings-support-row">
          <div>
            <strong>重新配置播放器、镜像与媒体来源</strong>
            <p>使用当前配置重新进入完整引导并逐项验证。进入引导不会清空现有配置，中途退出也不会影响当前软件使用。</p>
          </div>
          <GhostButton onClick={() => onOpenSetup?.()} disabled={!onOpenSetup}>
            <RotateCcw size={16} aria-hidden="true" />重新进入初始引导
          </GhostButton>
        </div>
      </SettingsSection>
      <SettingsSection title="问题记录与导出">
        <div className="settings-support-row">
          <div>
            <strong>错误日志</strong>
            <p>导入、刮削和任务错误全部如实记录在本机日志文件中（data/logs/error/）。点击导出可下载完整日志文本，便于分析问题。</p>
          </div>
          <GhostButton onClick={() => void runAction('导出错误日志', async () => {
            const text = await exportErrorLogText(90);
            const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            const date = new Date().toISOString().slice(0, 10);
            anchor.href = url;
            anchor.download = `kumiplayer-error-log-${date}.txt`;
            document.body.appendChild(anchor);
            anchor.click();
            document.body.removeChild(anchor);
            URL.revokeObjectURL(url);
            report('错误日志已导出');
          })}>
            <Download size={16} aria-hidden="true" />导出错误日志
          </GhostButton>
        </div>
      </SettingsSection>
      <SettingsSection title="支持与赞助">
        <div className="settings-support-row">
          <div>
            <strong>支持 KumiPlayer</strong>
            <p>博客与 GitHub 地址将在后续开放。正式地址确定前，不会跳转到临时或无效页面。</p>
          </div>
          <GhostButton onClick={() => undefined} disabled>内容稍后开放</GhostButton>
        </div>
      </SettingsSection>
    </PanelStack>
  );

  const contentByTab: Record<SettingsTab, ReactNode> = {
    appearance: renderAppearance(),
    scrape: renderScrape(),
    player: renderPlayer(),
    bangumi: renderBangumi(),
    support: renderSupport(),
  };

  return (
    <div className="settings-shell settings-shell-settings">
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
            <span>KumiPlayer 偏好与服务</span>
          </div>
          <div className="settings-outline-nav">
            {sectionTabs.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => selectSection(tab.key)}
                  className={`settings-outline-link ${activeSection === tab.key ? 'is-active' : ''}`}
                  aria-current={activeSection === tab.key ? 'location' : undefined}
                >
                  <span className="settings-outline-icon"><Icon size={17} strokeWidth={1.8} /></span>
                  <span className="settings-outline-copy"><strong>{tab.label}</strong><small>{tab.summary}</small></span>
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

function SourceSelect({ value, onChange }: { value: SourceKey; onChange: (value: SourceKey) => void }) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value as SourceKey)} className="settings-input w-36">
      <option value="pan115">115 网盘</option>
      <option value="baidu">百度网盘</option>
      <option value="local">本地</option>
    </select>
  );
}

function ConfigRow({ label, value, onSave, secret = false, placeholder = '' }: { label: string; value: string; onSave: (value: string) => void; secret?: boolean; placeholder?: string }) {
  const initialDraft = secret ? '' : value || '';
  const [draft, setDraft] = useState(initialDraft);
  useEffect(() => setDraft(secret ? '' : value || ''), [secret, value]);
  const resolvedPlaceholder = secret && value ? '已配置；粘贴新凭据可替换' : placeholder;
  return (
    <div className="settings-config-row">
      <label>{label}</label>
      <input type={secret ? 'password' : 'text'} value={draft} onChange={(event) => setDraft(event.target.value)} className="settings-input" placeholder={resolvedPlaceholder} autoComplete={secret ? 'off' : undefined} />
      <GhostButton onClick={() => onSave(draft)} disabled={secret && !draft.trim()}>保存</GhostButton>
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
  const [draft, setDraft] = useState(value || '');
  useEffect(() => setDraft(value || ''), [value]);
  return (
    <div className="settings-config-row">
      <label>{label}</label>
      <select value={draft} onChange={(event) => setDraft(event.target.value)} className="settings-input">
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
      <button onClick={onChange} className={`settings-switch ${active ? 'on' : ''}`} aria-label={label}>
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
