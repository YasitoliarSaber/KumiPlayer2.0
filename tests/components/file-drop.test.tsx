import { beforeEach, expect, test, vi } from 'vitest';

const tauriMocks = vi.hoisted(() => ({
  onDragDropEvent: vi.fn(),
}));

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: () => true,
}));

vi.mock('@tauri-apps/api/webview', () => ({
  getCurrentWebview: () => ({
    onDragDropEvent: tauriMocks.onDragDropEvent,
  }),
}));

import { listenForTreeFileDrop, listenForVideoFileDrop } from '../../src/platform/fileDrop';

beforeEach(() => {
  tauriMocks.onDragDropEvent.mockReset();
});

test('Tauri 单个 TXT 拖放会进入目录树监听器并保留原始路径', async () => {
  const cleanup = vi.fn();
  let nativeHandler: ((event: { payload: { type: string; paths?: string[] } }) => void) | undefined;
  tauriMocks.onDragDropEvent.mockImplementation(async (handler) => {
    nativeHandler = handler;
    return cleanup;
  });
  const handler = vi.fn();

  const unlisten = await listenForTreeFileDrop(handler);
  nativeHandler?.({ payload: { type: 'enter', paths: ['H:\\百度网盘\\新番_文件目录.TXT'] } });
  nativeHandler?.({ payload: { type: 'drop', paths: ['H:\\百度网盘\\新番_文件目录.TXT'] } });

  expect(handler).toHaveBeenNthCalledWith(1, {
    type: 'enter',
    paths: ['H:\\百度网盘\\新番_文件目录.TXT'],
  });
  expect(handler).toHaveBeenNthCalledWith(2, {
    type: 'drop',
    paths: ['H:\\百度网盘\\新番_文件目录.TXT'],
  });
  unlisten();
  expect(cleanup).toHaveBeenCalledOnce();
});

test('目录树监听器拒绝多个文件和非 TXT，视频监听器仍只接收视频', async () => {
  const nativeHandlers: Array<(event: { payload: { type: string; paths?: string[] } }) => void> = [];
  tauriMocks.onDragDropEvent.mockImplementation(async (handler) => {
    nativeHandlers.push(handler);
    return vi.fn();
  });
  const treeHandler = vi.fn();
  const videoHandler = vi.fn();

  await listenForTreeFileDrop(treeHandler);
  await listenForVideoFileDrop(videoHandler);
  nativeHandlers[0]({ payload: { type: 'drop', paths: ['D:\\a.txt', 'D:\\b.txt'] } });
  nativeHandlers[0]({ payload: { type: 'drop', paths: ['D:\\poster.png'] } });
  nativeHandlers[1]({ payload: { type: 'drop', paths: ['D:\\episode.mkv', 'D:\\note.txt'] } });

  expect(treeHandler).toHaveBeenNthCalledWith(1, { type: 'drop', paths: [] });
  expect(treeHandler).toHaveBeenNthCalledWith(2, { type: 'drop', paths: [] });
  expect(videoHandler).toHaveBeenCalledWith({ type: 'drop', paths: ['D:\\episode.mkv'] });
});
