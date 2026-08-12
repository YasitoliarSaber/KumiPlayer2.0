// KumiPlayer 2.0 Assets API

import { API_BASE } from './client'
import { withApiSessionToken } from './sessionToken'

type AssetKind = 'poster' | 'backdrop' | 'detailBackdrop' | 'candidate' | 'episode' | 'logo'

export function isRemoteAssetPath(path: string | null | undefined): boolean {
  if (!path) return false
  return /^https?:\/\//i.test(path) || /^\/[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)$/i.test(path)
}

export function buildAssetUrl(
  path: string | null | undefined,
  options: { kind?: AssetKind; thumbnailWidth?: number } = {},
): string {
  if (!path) return ''
  if (/^\/[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)$/i.test(path)) {
    return buildAssetUrl(`https://image.tmdb.org/t/p/original${path}`, { kind: options.kind })
  }
  if (/^https?:\/\//i.test(path)) {
    const normalized = normalizeRemoteImageUrl(path, options.kind)
    if (
      /^https:\/\/image\.tmdb\.org\/t\/p\//i.test(normalized)
      || /^https:\/\/s4\.anilist\.co\/file\/anilistcdn\//i.test(normalized)
    ) {
      return withApiSessionToken(API_BASE + '/api/assets/remote?url=' + encodeURIComponent(normalized))
    }
    return ''
  }
  // 本地镜像图片：分类页等小卡片场景走派生缩略图端点，降低解码与内存开销。
  // 远程图片已在前端归一到合适尺寸档，无需缩略图。
  const { thumbnailWidth } = options
  if (typeof thumbnailWidth === 'number' && thumbnailWidth > 0) {
    return withApiSessionToken(
      API_BASE + '/api/assets/thumbnail?path=' + encodeURIComponent(path) + '&width=' + thumbnailWidth,
    )
  }
  return withApiSessionToken(API_BASE + '/api/assets?path=' + encodeURIComponent(path))
}

function normalizeRemoteImageUrl(url: string, kind: AssetKind = 'poster'): string {
  const tmdbMatch = url.match(/^(https?:\/\/image\.tmdb\.org\/t\/p\/)([^/]+)(\/.+)$/i)
  if (!tmdbMatch) return url

  const size = kind === 'candidate'
    ? 'w185'
    : kind === 'episode'
      ? 'w500'
    : kind === 'detailBackdrop'
      ? 'original'
      : kind === 'backdrop'
        ? 'w1280'
        : kind === 'logo'
          ? 'w300'
          : 'w342'
  return tmdbMatch[1] + size + tmdbMatch[3]
}
