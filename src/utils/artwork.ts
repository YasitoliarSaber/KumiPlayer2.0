export type ArtworkKind = 'poster' | 'fanart' | 'clearlogo';

type ArtworkOwner = Partial<Record<
  'poster_path' | 'fanart_path' | 'clearlogo_path'
  | 'local_poster_path' | 'local_fanart_path' | 'local_clearlogo_path',
  string | null | undefined
>>;

/**
 * V4 保留远程元数据地址用于重新刮削，同时把已物化图片单独返回。
 * 阅读界面应优先使用本地副本，避免每次进入页面再次经过远程图片代理。
 */
export function preferredArtworkPath(owner: ArtworkOwner, kind: ArtworkKind): string {
  const paths: Record<ArtworkKind, [string | null | undefined, string | null | undefined]> = {
    poster: [owner.local_poster_path, owner.poster_path],
    fanart: [owner.local_fanart_path, owner.fanart_path],
    clearlogo: [owner.local_clearlogo_path, owner.clearlogo_path],
  };
  return String(paths[kind][0] || paths[kind][1] || '').trim();
}
