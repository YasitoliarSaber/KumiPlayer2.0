import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useUiStore } from './stores/ui';
import { useLibraryStore } from './stores/library';
import { useBangumiStore, SESSION_VERIFY_TTL_MS } from './stores/bangumi';
import { useConnectionStore } from './stores/connection';
import { useMediaWorkflowStore } from './stores/mediaWorkflow';
import AppShell from './components/shell/AppShell';
import StartupSplash from './components/shell/StartupSplash';
import HomePage from './pages/HomePage';
import FavoritesPage from './pages/FavoritesPage';
import RecentPage from './pages/RecentPage';
import CategoryPage from './pages/CategoryPage';
import SearchPage from './pages/SearchPage';
import LoadingState from './components/ui/loading-state';
import { configApi, type PublicConfig } from './api/config';
import { tasksApi } from './api/tasks';
import { FluentProvider } from '@fluentui/react-components';
import { getKumiFluentTheme } from './design/fluentTheme';
import { listenForTreeFileDrop } from './platform/fileDrop';

const WorkDetailPage = lazy(() => import('./pages/WorkDetailPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const PlayerTuningPage = lazy(() => import('./pages/PlayerTuningPage'));
const MediaManagementPage = lazy(() => import('./pages/MediaManagementPage'));
const FirstRunSetup = lazy(() => import('./pages/FirstRunSetup'));

export default function App() {
  const page = useUiStore((state) => state.page);
  const query = useUiStore((state) => state.query);
  const selectedWorkId = useUiStore((state) => state.selectedWorkId);
  const activeSource = useUiStore((state) => state.source);
  const styleMode = useUiStore((state) => state.styleMode);
  const motionMode = useUiStore((state) => state.motionMode);
  const appearanceMode = useUiStore((state) => state.appearanceMode);
  const setSeriesCardImageMode = useUiStore((state) => state.setSeriesCardImageMode);
  const setPosterSize = useUiStore((state) => state.setPosterSize);
  const goManage = useUiStore((state) => state.goManage);
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const { restoreSession, verifySession } = useBangumiStore();
  const { startHeartbeat, stopHeartbeat, startHealthPolling, stopHealthPolling } = useConnectionStore();
  const queueDroppedTreePath = useMediaWorkflowStore((state) => state.queueDroppedTreePath);
  const fluentTheme = useMemo(() => getKumiFluentTheme(appearanceMode), [appearanceMode]);
  const [appConfig, setAppConfig] = useState<PublicConfig | null>(null);
  const [configReady, setConfigReady] = useState(false);
  const [setupOverride, setSetupOverride] = useState(false);
  const completedScrapeTaskIdsRef = useRef<Set<string>>(new Set());
  const observedActiveScrapeTaskIdsRef = useRef<Set<string>>(new Set());
  const scrapeLibraryRevisionByTaskRef = useRef<Map<string, number>>(new Map());
  const scrapeWatcherStartedAtRef = useRef(Date.now());

  useLayoutEffect(() => {
    if (!configReady) return;
    document.getElementById('kumi-boot-splash')?.remove();
  }, [configReady]);

  useEffect(() => {
    document.documentElement.setAttribute('data-style', styleMode);
    document.documentElement.setAttribute('data-motion', motionMode);
    document.documentElement.setAttribute('data-theme', appearanceMode);
  }, [styleMode, motionMode, appearanceMode]);

  useEffect(() => {
    if (!appConfig?.setup_completed || setupOverride) return undefined;
    if (page === 'detail' || page === 'settings') return undefined;

    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listenForTreeFileDrop((event) => {
      if (event.type !== 'drop' || event.paths.length !== 1) return;
      queueDroppedTreePath(event.paths[0]);
      goManage();
    }).then((cleanup) => {
      if (disposed) cleanup();
      else unlisten = cleanup;
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [appConfig?.setup_completed, goManage, page, queueDroppedTreePath, setupOverride]);

  useEffect(() => {
    if (!appConfig?.setup_completed) return;
    loadLibrary();
  }, [appConfig?.setup_completed, loadLibrary]);

  useEffect(() => {
    if (!appConfig?.setup_completed) return undefined;
    let disposed = false;
    scrapeWatcherStartedAtRef.current = Date.now();

    const refreshAfterCompletedScrape = async () => {
      try {
        const payload = await tasksApi.list({ type_prefix: 'scrape_', limit: 12 });
        if (disposed) return;
        const tasks = payload.tasks || [];
        const activeTasks = tasks.filter((task) => task.status === 'pending' || task.status === 'running');
        for (const task of activeTasks) {
          observedActiveScrapeTaskIdsRef.current.add(task.task_id);
        }
        const publishedDuringScrape = activeTasks.filter((task) => {
          const result = (task.result || {}) as Record<string, unknown>;
          const revision = Math.max(0, Number(result.library_refresh_revision || 0));
          return revision > (scrapeLibraryRevisionByTaskRef.current.get(task.task_id) || 0);
        });
        if (publishedDuringScrape.length) {
          await loadLibrary({ force: true });
          if (!useLibraryStore.getState().error) {
            for (const task of publishedDuringScrape) {
              const result = (task.result || {}) as Record<string, unknown>;
              scrapeLibraryRevisionByTaskRef.current.set(
                task.task_id,
                Math.max(0, Number(result.library_refresh_revision || 0)),
              );
            }
          }
        }
        const completed = tasks.filter((task) => task.status === 'succeeded');
        for (const task of completed) {
          if (completedScrapeTaskIdsRef.current.has(task.task_id)) continue;
          const finishedAt = Date.parse(task.finished_at || '');
          const finishedInThisSession = Number.isFinite(finishedAt) && finishedAt >= scrapeWatcherStartedAtRef.current;
          const wasObservedActive = observedActiveScrapeTaskIdsRef.current.has(task.task_id);
          completedScrapeTaskIdsRef.current.add(task.task_id);
          if (wasObservedActive || finishedInThisSession) {
            await loadLibrary({ force: true });
          }
        }
      } catch {
        // 后台任务轮询不能阻断主界面；媒体管理页仍会显示更具体的任务错误。
      }
    };

    void refreshAfterCompletedScrape();
    const timer = window.setInterval(() => {
      void refreshAfterCompletedScrape();
    }, 4000);

    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [appConfig?.setup_completed, loadLibrary]);

  useEffect(() => {
    if (!appConfig?.setup_completed) return undefined;
    startHeartbeat();
    startHealthPolling();
    return () => {
      stopHealthPolling();
      stopHeartbeat();
    };
  }, [appConfig?.setup_completed, startHeartbeat, startHealthPolling, stopHeartbeat, stopHealthPolling]);

  useEffect(() => {
    if (!appConfig?.setup_completed) return undefined;
    let disposed = false;
    // 启动只做本地会话恢复（GET /session，0 远程请求、不重试）；
    // 认证证据时间取 max(last_verified_at, last_success_at)：任何 authenticated
    // 业务请求成功（_observe_auth 更新 last_success_at）都计入 TTL，避免刚同步
    // 完又触发一次 /v0/me；仅超 TTL 时后台执行一次远程验证（失败不阻塞 UI）
    void restoreSession().then((session) => {
      if (disposed || !session || session.credential_state !== 'found') return;
      const lastVerified = session.last_verified_at ? new Date(session.last_verified_at).getTime() : 0;
      const lastSuccess = session.last_success_at ? new Date(session.last_success_at).getTime() : 0;
      const effective = Math.max(lastVerified, lastSuccess);
      const stale = !effective || Date.now() - effective > SESSION_VERIFY_TTL_MS;
      if (stale) {
        void verifySession().catch(() => {
          // 后台验证失败保留本地会话，账户卡不受影响
        });
      }
    });
    return () => {
      disposed = true;
    };
  }, [appConfig?.setup_completed, restoreSession, verifySession]);

  useEffect(() => {
    configApi.getConfig()
      .then((config) => {
        setAppConfig(config);
        if (config.series_card_image_mode === 'poster' || config.series_card_image_mode === 'fanart') {
          setSeriesCardImageMode(config.series_card_image_mode);
        }
        if (typeof config.poster_size === 'number') {
          setPosterSize(config.poster_size);
        }
      })
      .catch(() => {
        // 本地存储的 UI 偏好仍可兜底，配置读取失败不阻断首页。
      })
      .finally(() => {
        setConfigReady(true);
      });
  }, [setSeriesCardImageMode, setPosterSize]);

  const renderPage = () => {
    switch (page) {
      case 'home':
        return <HomePage />;
      case 'favorites':
        return <FavoritesPage />;
      case 'recent':
        return <RecentPage />;
      case 'category':
        return <CategoryPage />;
      case 'detail':
        return <WorkDetailPage key={selectedWorkId || 'detail'} />;
      case 'manage':
        return <MediaManagementPage />;
      case 'settings':
        return <SettingsPage onOpenSetup={() => setSetupOverride(true)} />;
      case 'player-tuning':
        return <PlayerTuningPage />;
      default:
        return <HomePage />;
    }
  };

  const visiblePage = query.trim() ? 'search' : page;
  return (
    <FluentProvider theme={fluentTheme}>
      {!configReady && <StartupSplash />}
      {configReady && appConfig && (!appConfig.setup_completed || setupOverride) && (
        <Suspense fallback={<LoadingState label="正在准备初始设置" detail="正在载入本机配置" />}>
          <FirstRunSetup
            initialConfig={appConfig}
            mode={setupOverride ? "reconfigure" : "first-run"}
            onCancel={setupOverride ? () => setSetupOverride(false) : undefined}
            onComplete={(config) => {
              setAppConfig(config);
              setSetupOverride(false);
            }}
          />
        </Suspense>
      )}
      {configReady && (!appConfig || (appConfig.setup_completed && !setupOverride)) && (
        <AppShell>
          <div className="app-page-source-scope" data-source={activeSource}>
            <Suspense fallback={null}>
              <div
                key={visiblePage}
                className="app-page-transition"
                data-page={visiblePage}
              >
                {visiblePage === 'search' ? <SearchPage /> : renderPage()}
              </div>
            </Suspense>
          </div>
        </AppShell>
      )}
    </FluentProvider>
  );
}
