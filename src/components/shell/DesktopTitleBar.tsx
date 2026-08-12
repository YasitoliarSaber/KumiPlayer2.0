import { useState, useCallback, useEffect, useRef } from 'react';
import type { MouseEvent } from 'react';
import { ArrowLeft, ArrowRight, Check, ListFilter, PanelLeft, RefreshCcw, Search, X } from 'lucide-react';
import { isTauri } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { useUiStore, type SourceId } from '../../stores/ui';
import { useLibraryStore } from '../../stores/library';

const sourceOptions: Array<{ value: SourceId; label: string }> = [
  { value: 'all', label: '全部来源' },
  { value: 'pan115', label: '115 网盘' },
  { value: 'baidu', label: '百度网盘' },
  { value: 'openlist', label: 'OpenList 连接' },
  { value: 'local', label: '本地' },
];

export default function DesktopTitleBar() {
  const [maximized, setMaximized] = useState(false);
  const [sourceMenuOpen, setSourceMenuOpen] = useState(false);
  const sourceMenuRef = useRef<HTMLDivElement>(null);
  const {
    query,
    setQuery,
    page,
    source,
    setSource,
    canGoBack,
    canGoForward,
    goBack,
    goForward,
    sidebarMode,
    toggleSidebarVisibility,
  } = useUiStore();
  const loadLibrary = useLibraryStore((state) => state.loadLibrary);
  const libraryLoading = useLibraryStore((state) => state.loading);
  const tauri = isTauri();
  const appWindow = tauri ? getCurrentWindow() : null;

  const stopDrag = useCallback((event: MouseEvent<HTMLElement>) => {
    event.stopPropagation();
  }, []);

  const handleMinimize = useCallback(() => {
    void appWindow?.minimize();
  }, [appWindow]);

  const handleToggleMaximize = useCallback(async () => {
    if (!appWindow) return;
    await appWindow.toggleMaximize();
    setMaximized(await appWindow.isMaximized());
  }, [appWindow]);

  const handleClose = useCallback(() => {
    void appWindow?.close();
  }, [appWindow]);

  useEffect(() => {
    if (!appWindow) return;
    void appWindow.isMaximized().then(setMaximized);
  }, [appWindow]);

  useEffect(() => {
    if (!sourceMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!sourceMenuRef.current?.contains(event.target as Node)) setSourceMenuOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [sourceMenuOpen]);

  const activeSourceLabel = sourceOptions.find((option) => option.value === source)?.label ?? '全部来源';
  const sidebarHidden = sidebarMode === 'hidden';
  const sidebarVisibilityLabel = sidebarHidden ? '显示导航栏' : '完全隐藏导航栏';

  return (
    <header className="desktop-titlebar" data-tauri-drag-region={tauri || undefined}>
      <div className="desktop-titlebar-leading">
        <button
          type="button"
          className="titlebar-navigation-button titlebar-sidebar-visibility-button"
          onMouseDown={stopDrag}
          onClick={toggleSidebarVisibility}
          aria-controls="sidebar-primary-navigation"
          aria-expanded={!sidebarHidden}
          aria-label={sidebarVisibilityLabel}
          title={sidebarVisibilityLabel}
        >
          <PanelLeft aria-hidden="true" />
        </button>
        <button
          type="button"
          className="titlebar-navigation-button"
          onMouseDown={stopDrag}
          onClick={goBack}
          disabled={!canGoBack}
          title={canGoBack ? '返回上个界面' : '没有可返回的界面'}
          aria-label="返回上个界面"
        >
          <ArrowLeft aria-hidden="true" />
        </button>
        <button
          type="button"
          className="titlebar-navigation-button"
          onMouseDown={stopDrag}
          onClick={goForward}
          disabled={!canGoForward}
          title={canGoForward ? '前进到下个界面' : '没有可前进的界面'}
          aria-label="前进到下个界面"
        >
          <ArrowRight aria-hidden="true" />
        </button>
        <div className="desktop-titlebar-brand">
          <img className="desktop-titlebar-mark" src="/brand/kumiplayer-app-icon.svg" alt="" aria-hidden="true" />
          <span className="desktop-titlebar-wordmark">KumiPlayer</span>
        </div>
      </div>
      <div className="desktop-titlebar-tools">
        {page === 'category' && (
          <button
            type="button"
            className="titlebar-library-refresh"
            title="刷新媒体库"
            aria-label="刷新媒体库"
            aria-busy={libraryLoading}
            disabled={libraryLoading}
            onMouseDown={stopDrag}
            onClick={() => void loadLibrary({ force: true })}
          >
            <RefreshCcw aria-hidden="true" size={17} />
          </button>
        )}
        <div className="titlebar-source-filter" ref={sourceMenuRef}>
          <button
            type="button"
            className={`titlebar-source-filter-trigger ${source !== 'all' ? 'is-filtered' : ''}`}
            title={`来源：${activeSourceLabel}`}
            aria-label={`来源筛选，当前${activeSourceLabel}`}
            aria-haspopup="menu"
            aria-expanded={sourceMenuOpen}
            onMouseDown={stopDrag}
            onClick={() => setSourceMenuOpen((open) => !open)}
          >
            <ListFilter aria-hidden="true" size={17} />
          </button>
          {sourceMenuOpen && (
            <div className="titlebar-source-filter-menu" role="menu" aria-label="来源筛选">
              {sourceOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="menuitemradio"
                  aria-checked={source === option.value}
                  className={source === option.value ? 'active' : ''}
                  onMouseDown={stopDrag}
                  onClick={() => {
                    setSource(option.value);
                    setSourceMenuOpen(false);
                  }}
                >
                  <span>{option.label}</span>
                  {source === option.value && <Check aria-hidden="true" size={15} />}
                </button>
              ))}
            </div>
          )}
        </div>
        <label className="desktop-titlebar-search" onMouseDown={stopDrag}>
          <Search aria-hidden="true" size={16} />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索作品"
            aria-label="搜索作品"
          />
          {query && (
            <button
              type="button"
              className="desktop-titlebar-search-clear"
              onMouseDown={stopDrag}
              onClick={() => setQuery('')}
              aria-label="清空搜索"
            >
              <X aria-hidden="true" size={15} />
            </button>
          )}
        </label>
      </div>
      <div className="desktop-window-controls">
        <button className="desktop-window-btn" onMouseDown={stopDrag} onClick={handleMinimize} aria-label="Minimize">
          <svg viewBox="0 0 12 12"><path d="M2 6.5h8" /></svg>
        </button>
        <button className="desktop-window-btn" onMouseDown={stopDrag} onClick={handleToggleMaximize} aria-label="Maximize">
          <svg viewBox="0 0 12 12">
            {maximized ? (
              <><rect x="2.5" y="3.5" width="6" height="6" /><path d="M4 3.5V2h6v6H8.5" /></>
            ) : (
              <rect x="2.5" y="2.5" width="7" height="7" />
            )}
          </svg>
        </button>
        <button className="desktop-window-btn close" onMouseDown={stopDrag} onClick={handleClose} aria-label="Close">
          <svg viewBox="0 0 12 12"><path d="m2.5 2.5 7 7m0-7-7 7" /></svg>
        </button>
      </div>
    </header>
  );
}
