import { create } from 'zustand'
import { libraryApi } from '../api/library'
import { playbackApi } from '../api/playback'
import { useUiStore, type SourceId } from './ui'
import { matchesSourceFilter } from '../utils/sourceFilter'
import type { WorkIndex, PlaybackHistoryItem } from '../api/types'
import { mainEpisodeCount } from '../utils/workStats'
import { isWorkInLibraryView } from '../utils/libraryCategories'
import { buildAssetUrl } from '../api/assets'

const LIBRARY_CACHE_KEY = 'kumiplayer-library-cache-v4'
const LEGACY_LIBRARY_CACHE_KEYS = [
  'kumiplayer-library-cache-v3',
  'kumiplayer-library-cache-v2',
  'kumiplayer-library-cache-v1',
]
const LIBRARY_CACHE_MAX_AGE = 1000 * 60 * 60 * 24 * 7
const LIBRARY_CACHE_MAX_BYTES = 1_500_000
const LIBRARY_CACHE_MAX_WORKS = 500
const DETAIL_ARTWORK_WAIT_MS = 120

interface LibraryCachePayload {
  savedAt: number
  works: WorkIndex[]
}

interface LibraryState {
  works: WorkIndex[]
  history: PlaybackHistoryItem[]
  loading: boolean
  error: string | null
  loaded: boolean

  selectedWorkDetail: WorkIndex | null
  detailLoading: boolean
  detailError: string | null

  filteredWorks: () => WorkIndex[]
  sortedWorks: () => WorkIndex[]
  selectedWork: () => WorkIndex | null
  librarySummary: () => { workCount: number; episodeCount: number }

  loadLibrary: (options?: { force?: boolean }) => Promise<void>
  openWorkDetail: (workId: string) => Promise<void>
  closeWorkDetail: () => void
  refreshHistory: () => Promise<void>
  updateWorkWatchStatus: (workId: string, watchStatus: WorkIndex['watch_status']) => void
  peekWorkDetail: (workId: string) => WorkIndex | null
  getWorkDetail: (workId: string) => Promise<any>
  invalidateWork: (workId: string) => void
  refreshWork: (workId: string) => Promise<WorkIndex>
}

const detailCache = new Map<string, WorkIndex>()
const detailRequests = new Map<string, Promise<WorkIndex>>()
const detailArtworkRequests = new Map<string, Promise<void>>()
let detailNavigationRequest = 0
let libraryLoadRequest = 0
let libraryCacheGeneration = 0

type WorkIndexWithLocalClearlogo = WorkIndex & { local_clearlogo_path?: string }

function mergeWorkDetailIntoCompact(previous: WorkIndex, detail: WorkIndex): WorkIndex {
  const compact = previous as WorkIndexWithLocalClearlogo
  const next = detail as WorkIndexWithLocalClearlogo
  return {
    ...previous,
    ...detail,
    // 详情返回值缺少 local_* 时保留 compact 卡片中的本地图片路径，避免返回分类页后丢失本地图。
    local_poster_path: next.local_poster_path || compact.local_poster_path,
    local_fanart_path: next.local_fanart_path || compact.local_fanart_path,
    local_clearlogo_path: next.local_clearlogo_path || compact.local_clearlogo_path,
  } as WorkIndex
}

type ViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void) => unknown
}

function commitDetailNavigation(workId: string) {
  const update = () => {
    useUiStore.getState().goDetail(workId)
    document.querySelector<HTMLElement>('.app-main')?.scrollTo({ top: 0, behavior: 'auto' })
  }
  const documentWithTransition = document as ViewTransitionDocument
  const reduceMotion = document.documentElement.dataset.motion === 'reduced'
    || window.matchMedia('(prefers-reduced-motion: reduce)').matches

  if (reduceMotion || !documentWithTransition.startViewTransition) {
    update()
    return
  }

  documentWithTransition.startViewTransition(update)
}

function decodeDetailArtwork(work: WorkIndex): Promise<void> {
  const path = work.fanart_path || work.poster_path || ''
  if (!path || typeof Image === 'undefined') return Promise.resolve()

  const url = buildAssetUrl(path, { kind: 'detailBackdrop' })
  const existing = detailArtworkRequests.get(url)
  if (existing) return existing

  const image = new Image()
  image.decoding = 'async'
  image.src = url
  const request = image.decode().catch(() => undefined)
  detailArtworkRequests.set(url, request)
  return request
}

async function waitForDetailArtwork(work: WorkIndex, timeoutMs: number) {
  let timeoutId = 0
  const timeout = new Promise<void>((resolve) => {
    timeoutId = window.setTimeout(resolve, timeoutMs)
  })
  await Promise.race([decodeDetailArtwork(work), timeout])
  window.clearTimeout(timeoutId)
}

function deduplicateWorks(works: WorkIndex[]) {
  const selected = new Map<string, WorkIndex>()
  for (const work of works) {
    const key = `${work.source}:${work.work_id}`
    const previous = selected.get(key)
    if (!previous || workCompleteness(work) >= workCompleteness(previous)) {
      selected.set(key, work)
    }
  }
  return [...selected.values()]
}

function workCompleteness(work: WorkIndex) {
  const seasonEpisodes = (work.seasons || []).reduce((total, season) => total + (season.episode_count || 0), 0)
  return (work.episodes?.length || 0) * 10_000 + seasonEpisodes * 10 + (work.seasons?.length || 0)
}

export const useLibraryStore = create<LibraryState>((set, get) => ({
  works: [],
  history: [],
  loading: false,
  error: null,
  loaded: false,
  selectedWorkDetail: null,
  detailLoading: false,
  detailError: null,

  filteredWorks: () => {
    const ui = useUiStore.getState()
    let result = [...get().works]

    if (ui.source !== 'all') {
      const activeSource = ui.source as Exclude<SourceId, 'all'>
      result = result.filter((w) => matchesSourceFilter(w, activeSource))
    }

    if (ui.activeCategory) {
      result = result.filter((w) => isWorkInLibraryView(w, ui.activeCategory!))
    }

    if (ui.query) {
      const q = ui.query.toLowerCase()
      result = result.filter(
        (w) =>
          w.title.toLowerCase().includes(q) ||
          w.original_title.toLowerCase().includes(q) ||
          (w.related_works?.some((r) => r.title.toLowerCase().includes(q)) ?? false)
      )
    }

    return result
  },

  sortedWorks: () => {
    const ui = useUiStore.getState()
    const list = [...get().filteredWorks()]

    switch (ui.sort) {
      case 'recent': {
        const historyMap = new Map<string, number>()
        get().history.forEach((h, i) => {
          if (!historyMap.has(h.work_id)) historyMap.set(h.work_id, i)
        })
        return list.sort((a, b) => {
          const ra = historyMap.get(a.work_id) ?? Infinity
          const rb = historyMap.get(b.work_id) ?? Infinity
          return ra - rb
        })
      }
      case 'ratingDesc':
        return list.sort((a, b) => (b.rating || 0) - (a.rating || 0))
      case 'ratingAsc':
        return list.sort((a, b) => (a.rating || 0) - (b.rating || 0))
      case 'episodesDesc':
        return list.sort((a, b) => mainEpisodeCount(b) - mainEpisodeCount(a))
      case 'episodesAsc':
        return list.sort((a, b) => mainEpisodeCount(a) - mainEpisodeCount(b))
      case 'yearAsc':
        return list.sort((a, b) => (a.year ?? 9999) - (b.year ?? 9999))
      case 'yearDesc':
        return list.sort((a, b) => (b.year ?? 0) - (a.year ?? 0))
      default:
        return list
    }
  },

  selectedWork: () => {
    const st = get()
    if (st.selectedWorkDetail) return st.selectedWorkDetail
    const ui = useUiStore.getState()
    if (!ui.selectedWorkId) return null
    return st.works.find((w) => w.work_id === ui.selectedWorkId) ?? null
  },

  librarySummary: () => {
    const works = get().filteredWorks()
    return {
      workCount: works.length,
      episodeCount: works.reduce((sum, w) => sum + mainEpisodeCount(w), 0),
    }
  },

  loadLibrary: async (options) => {
    const force = options?.force === true
    if (get().loading && !force) return
    const requestId = ++libraryLoadRequest

    if (!force) {
      hydrateLibraryCache(set, get)
    } else {
      detailCache.clear()
    }

    const hasUsableContent = get().works.length > 0
    set({ loading: !hasUsableContent, error: null })

    const libraryRequest = libraryApi.getLibrary({ compact: true }).then((libraryRes) => {
      if (requestId !== libraryLoadRequest) return

      const works = deduplicateWorks(libraryRes.works)
      set({
        works,
        loaded: true,
        loading: false,
        error: null,
      })
      saveLibraryCache(works)
    })
    const historyRequest = playbackApi.getHistory(20).then((historyRes) => {
      if (requestId !== libraryLoadRequest) return
      set({ history: historyRes.items })
    }).catch(() => {
      // 播放历史是首页增强信息；失败时保留已有历史和可用媒体库内容。
      return undefined
    })

    void historyRequest
    try {
      await libraryRequest
    } catch (cause: unknown) {
      if (requestId !== libraryLoadRequest) return
      set({
        error: cause instanceof Error ? cause.message : '加载失败',
        loading: false,
      })
    }
  },

  openWorkDetail: async (workId) => {
    const requestId = ++detailNavigationRequest
    set({ detailError: null, detailLoading: true })

    try {
      const work = await get().getWorkDetail(workId)
      if (requestId !== detailNavigationRequest) return
      await waitForDetailArtwork(work, DETAIL_ARTWORK_WAIT_MS)
      if (requestId !== detailNavigationRequest) return

      set({ selectedWorkDetail: work, detailLoading: false })
      commitDetailNavigation(workId)

      const works = get().works
      const index = works.findIndex((w) => w.work_id === workId)
      if (index >= 0) {
        const newWorks = [...works]
        newWorks[index] = mergeWorkDetailIntoCompact(works[index], work)
        set({ works: newWorks })
      }
    } catch (e: unknown) {
      if (requestId !== detailNavigationRequest) return
      set({
        detailError: e instanceof Error ? e.message : '加载详情失败',
        detailLoading: false,
      })
      // 网络失败时仍允许用媒体库索引中的精简信息进入详情页并展示错误提示。
      commitDetailNavigation(workId)
    }
  },

  closeWorkDetail: () => {
    useUiStore.getState().goHome()
    set({ selectedWorkDetail: null, detailError: null })
  },

  refreshHistory: async () => {
    try {
      const res = await playbackApi.getHistory(20)
      set({ history: res.items })
    } catch {
      // ignore
    }
  },

  updateWorkWatchStatus: (workId, watchStatus) => {
    set((state) => ({
      works: state.works.map((work) => work.work_id === workId ? { ...work, watch_status: watchStatus } : work),
      selectedWorkDetail: state.selectedWorkDetail?.work_id === workId
        ? { ...state.selectedWorkDetail, watch_status: watchStatus }
        : state.selectedWorkDetail,
    }))
  },

  peekWorkDetail: (workId) => detailCache.get(workId) || get().works.find((work) => work.work_id === workId) || null,

  getWorkDetail: async (workId: string) => {
    const cached = detailCache.get(workId)
    if (cached) {
      void decodeDetailArtwork(cached)
      return cached
    }

    let request = detailRequests.get(workId)
    if (!request) {
      request = libraryApi.getWorkDetail(workId)
      detailRequests.set(workId, request)
    }

    try {
      const work = await request
      detailRequests.delete(workId)
      detailCache.set(workId, work)
      void decodeDetailArtwork(work)
      return work
    } catch (e) {
      detailRequests.delete(workId)
      throw e
    }
  },

  invalidateWork: (workId) => {
    detailCache.delete(workId)
    detailRequests.delete(workId)
  },

  refreshWork: async (workId) => {
    detailCache.delete(workId)
    detailRequests.delete(workId)
    const work = await libraryApi.getWorkDetail(workId)
    detailCache.set(workId, work)
    const works = [...get().works]
    const index = works.findIndex((item) => item.work_id === workId)
    if (index >= 0) works[index] = mergeWorkDetailIntoCompact(works[index], work)
    else works.push(work)
    const normalizedWorks = deduplicateWorks(works)
    set({ works: normalizedWorks, selectedWorkDetail: work })
    saveLibraryCache(normalizedWorks)
    return work
  },
}))

function hydrateLibraryCache(
  set: (partial: Partial<LibraryState>) => void,
  get: () => LibraryState
) {
  if (get().loaded || get().works.length > 0 || typeof localStorage === 'undefined') return
  clearLegacyLibraryCaches()
  try {
    const raw = localStorage.getItem(LIBRARY_CACHE_KEY)
    if (!raw) return
    if (raw.length > LIBRARY_CACHE_MAX_BYTES) {
      localStorage.removeItem(LIBRARY_CACHE_KEY)
      return
    }
    const cached = JSON.parse(raw) as LibraryCachePayload
    if (!cached?.works?.length) return
    if (cached.works.length > LIBRARY_CACHE_MAX_WORKS) {
      localStorage.removeItem(LIBRARY_CACHE_KEY)
      return
    }
    if (Date.now() - cached.savedAt > LIBRARY_CACHE_MAX_AGE) return
    set({ works: deduplicateWorks(cached.works), loaded: true })
  } catch {
    localStorage.removeItem(LIBRARY_CACHE_KEY)
  }
}

function saveLibraryCache(works: WorkIndex[]) {
  const generation = ++libraryCacheGeneration
  if (typeof localStorage === 'undefined') return
  clearLegacyLibraryCaches()
  if (works.length === 0) {
    localStorage.removeItem(LIBRARY_CACHE_KEY)
    return
  }
  if (works.length > LIBRARY_CACHE_MAX_WORKS) {
    localStorage.removeItem(LIBRARY_CACHE_KEY)
    return
  }
  try {
    const payload: LibraryCachePayload = { savedAt: Date.now(), works }
    const serialized = JSON.stringify(payload)
    if (serialized.length > LIBRARY_CACHE_MAX_BYTES) {
      localStorage.removeItem(LIBRARY_CACHE_KEY)
      return
    }
    const idleWindow = window as Window & typeof globalThis & {
      requestIdleCallback?: (callback: IdleRequestCallback, options?: IdleRequestOptions) => number
    }
    const write = () => {
      if (generation === libraryCacheGeneration) {
        localStorage.setItem(LIBRARY_CACHE_KEY, serialized)
      }
    }
    if (idleWindow.requestIdleCallback) {
      idleWindow.requestIdleCallback(write, { timeout: 1000 })
    } else {
      globalThis.setTimeout(write, 0)
    }
  } catch {
    // ignore
  }
}

function clearLegacyLibraryCaches() {
  try {
    for (const key of LEGACY_LIBRARY_CACHE_KEYS) {
      localStorage.removeItem(key)
    }
  } catch {
    // ignore
  }
}
