import { isTauri } from '@tauri-apps/api/core';

const VIDEO_EXTENSIONS = new Set(['mkv', 'mp4', 'avi', 'ts', 'm2ts', 'wmv', 'flv', 'rmvb', 'mov']);

export type VideoFileDropEvent = {
  type: 'enter' | 'leave' | 'drop';
  paths: string[];
};

export type TreeFileDropEvent = VideoFileDropEvent;

function videoPaths(paths: string[]): string[] {
  return paths.filter((path) => {
    const extension = path.split('.').at(-1)?.toLowerCase() || '';
    return VIDEO_EXTENSIONS.has(extension);
  });
}

function treePaths(paths: string[]): string[] {
  if (paths.length !== 1) return [];
  const extension = paths[0].split('.').at(-1)?.toLowerCase() || '';
  return extension === 'txt' ? paths : [];
}

async function listenForFilteredFileDrop(
  filterPaths: (paths: string[]) => string[],
  handler: (event: VideoFileDropEvent) => void,
): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { getCurrentWebview } = await import('@tauri-apps/api/webview');
  return getCurrentWebview().onDragDropEvent((event) => {
    if (event.payload.type === 'enter') {
      handler({ type: 'enter', paths: filterPaths(event.payload.paths) });
      return;
    }
    if (event.payload.type === 'drop') {
      handler({ type: 'drop', paths: filterPaths(event.payload.paths) });
      return;
    }
    if (event.payload.type === 'leave') handler({ type: 'leave', paths: [] });
  });
}

/** 监听桌面壳原生文件拖放；浏览器预览环境保持无操作。 */
export async function listenForVideoFileDrop(
  handler: (event: VideoFileDropEvent) => void,
): Promise<() => void> {
  return listenForFilteredFileDrop(videoPaths, handler);
}

/** 监听单个 TXT 目录树拖放；来源格式由后端读取正文后判断。 */
export async function listenForTreeFileDrop(
  handler: (event: TreeFileDropEvent) => void,
): Promise<() => void> {
  return listenForFilteredFileDrop(treePaths, handler);
}
