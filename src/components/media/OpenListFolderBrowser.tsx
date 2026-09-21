import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Spinner } from '@fluentui/react-components'
import { ArrowSync24Regular, Cloud24Regular, FolderOpen24Regular } from '@fluentui/react-icons'
import { openlistApi, type OpenListBrowseResult } from '../../api/openlist'

interface OpenListFolderBrowserProps {
  configured: boolean
  initialPath: string
  onPathChange: (path: string) => void
  onLoadingChange?: (loading: boolean) => void
  onGoSettings: () => void
}

function normalizePath(path: string) {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  return parts.length ? `/${parts.join('/')}` : '/'
}

function crumbsFor(path: string, root: string) {
  const normalizedRoot = normalizePath(root)
  const normalizedPath = normalizePath(path)
  const rootParts = normalizedRoot.split('/').filter(Boolean)
  const pathParts = normalizedPath.split('/').filter(Boolean)
  const relativeParts = pathParts.slice(rootParts.length)
  const crumbs = [{ label: 'OpenList', path: normalizedRoot }]
  let current = normalizedRoot === '/' ? '' : normalizedRoot
  for (const part of relativeParts) {
    current = normalizePath(`${current}/${part}`)
    crumbs.push({ label: part, path: current })
  }
  return crumbs
}

export default function OpenListFolderBrowser({
  configured,
  initialPath,
  onPathChange,
  onLoadingChange,
  onGoSettings,
}: OpenListFolderBrowserProps) {
  const [result, setResult] = useState<OpenListBrowseResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const initialized = useRef(false)
  // P-004：浏览请求 generation 竞态保护——只有最新请求可以提交状态；
  // 快速进入慢目录再返回时，较慢的旧响应不能覆盖新目录。
  const generationRef = useRef(0)

  useEffect(() => {
    return () => {
      generationRef.current += 1
    }
  }, [])

  const browse = async (path: string, page = 1, refresh = false, append = false) => {
    const generation = ++generationRef.current
    append ? setLoadingMore(true) : setLoading(true)
    onLoadingChange?.(true)
    setError('')
    try {
      const next = await openlistApi.browse(path, page, refresh, 100)
      if (generation !== generationRef.current) return
      setResult((current) => append && current
        ? { ...next, entries: [...current.entries, ...next.entries] }
        : next)
      onPathChange(next.path)
    } catch (cause) {
      if (generation !== generationRef.current) return
      setError(cause instanceof Error ? cause.message : '无法读取 OpenList 目录')
    } finally {
      if (generation !== generationRef.current) return
      setLoading(false)
      setLoadingMore(false)
      onLoadingChange?.(false)
    }
  }

  useEffect(() => {
    if (!configured || initialized.current) return
    initialized.current = true
    void browse(initialPath || '/')
  }, [configured, initialPath])

  const crumbs = useMemo(
    () => crumbsFor(result?.path || initialPath || '/', result?.remote_root || '/'),
    [initialPath, result?.path, result?.remote_root],
  )

  if (!configured) {
    return (
      <section className="media-openlist-browser media-v4-openlist-browser" aria-label="OpenList 目录浏览器">
        <div className="media-openlist-unconfigured">
          <Cloud24Regular />
          <div><strong>尚未配置 OpenList</strong><span>请先完成连接、挂载根和内容来源路由设置。</span></div>
          <Button appearance="primary" onClick={onGoSettings}>前往 OpenList 设置</Button>
        </div>
      </section>
    )
  }

  return (
    <section className="media-openlist-browser media-v4-openlist-browser" aria-label="OpenList 目录浏览器">
      <div className="media-openlist-toolbar">
        <nav className="media-openlist-crumbs" aria-label="远端目录面包屑">
          {crumbs.map((crumb, index) => (
            <span key={crumb.path}>
              {index > 0 && <span aria-hidden="true">›</span>}
              {index < crumbs.length - 1
                ? <button type="button" onClick={() => void browse(crumb.path)}>{crumb.label}</button>
                : <strong>{crumb.label}</strong>}
            </span>
          ))}
        </nav>
        <div className="media-v4-browser-actions">
          <Button appearance="secondary" disabled={!result?.parent_path || loading} onClick={() => result?.parent_path && void browse(result.parent_path)}>上一级</Button>
          <Button appearance="secondary" icon={<ArrowSync24Regular />} disabled={loading} onClick={() => void browse(result?.path || initialPath || '/', 1, true)}>刷新当前层</Button>
        </div>
      </div>

      {error && <div className="media-flow-alert error" role="alert"><span>{error}</span><Button appearance="secondary" size="small" onClick={() => void browse(result?.path || initialPath || '/', 1, true)}>重试</Button></div>}
      {result?.cache.status === 'stale' && !loading && <div className="media-openlist-cache-hint" role="status"><ArrowSync24Regular aria-hidden="true" /><span>当前显示的是缓存的目录列表，远端可能已变化；可点击“刷新当前层”获取最新内容。</span></div>}
      {result?.cache.refresh_failed && <div className="media-openlist-cache-hint refresh-failed" role="status"><span>缓存刷新失败，仍在显示上次成功读取的目录列表。</span></div>}
      {result?.cache.refreshing && <div className="media-openlist-cache-hint" role="status"><Spinner size="tiny" /><span>正在刷新目录缓存…</span></div>}
      {loading && <div className="media-openlist-loading"><Spinner size="small" />正在读取目录…</div>}
      {!loading && result && (
        <>
          <div className="media-openlist-entries" role="list" aria-label="远端目录条目">
            {result.entries.map((entry) => entry.is_dir ? (
              <div className="media-openlist-entry dir" key={entry.remote_path} role="listitem">
                <button
                  type="button"
                  aria-label={`打开文件夹 ${entry.name}`}
                  onClick={() => void browse(entry.remote_path)}
                >
                  <FolderOpen24Regular /><span>{entry.name}</span><span className="media-openlist-entry-arrow" aria-hidden="true">›</span>
                </button>
              </div>
            ) : (
              <div className="media-openlist-entry file" role="listitem" key={entry.remote_path}>
                <span className="media-openlist-file-icon" /><span>{entry.name}</span>
              </div>
            ))}
          </div>
          {result.entries.length === 0 && <div className="media-v4-browser-empty">此目录没有可浏览的内容。</div>}
          <div className="media-openlist-foot">
            <span title={result.path}>当前目录：{result.path} · 已加载 {result.entries.length}{result.total > 0 ? ` / ${result.total}` : ''} 项{result.cache.status === 'fresh' && !result.cache.refreshing ? ' · 缓存有效' : result.cache.status === 'none' && !result.cache.refreshing ? ' · 远端最新' : ''}</span>
            {result.has_more && <Button appearance="secondary" disabled={loadingMore} onClick={() => void browse(result.path, result.page + 1, false, true)}>{loadingMore ? '正在加载…' : '加载更多'}</Button>}
          </div>
        </>
      )}
    </section>
  )
}
