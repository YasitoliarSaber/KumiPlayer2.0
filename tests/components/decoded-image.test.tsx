import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import DecodedImage from '../../src/components/ui/DecodedImage';

test('图片在浏览器解码完成前保持隐藏，完成后再一次性显示', async () => {
  let finishDecode: (() => void) | undefined;
  const decode = vi.fn(() => new Promise<void>((resolve) => {
    finishDecode = resolve;
  }));
  Object.defineProperty(HTMLImageElement.prototype, 'decode', {
    configurable: true,
    value: decode,
  });

  render(<DecodedImage src="http://127.0.0.1/poster.jpg" alt="测试海报" />);
  const image = screen.getByRole('img', { name: '测试海报' });

  expect(image).toHaveAttribute('data-image-state', 'loading');
  fireEvent.load(image);
  expect(decode).toHaveBeenCalled();
  expect(image).toHaveAttribute('data-image-state', 'loading');

  finishDecode?.();
  await waitFor(() => expect(image).toHaveAttribute('data-image-state', 'ready'));
});

test('图片完成解码后才通知调用方参与界面切换', async () => {
  let finishDecode: (() => void) | undefined;
  const onDecoded = vi.fn();
  Object.defineProperty(HTMLImageElement.prototype, 'decode', {
    configurable: true,
    value: vi.fn(() => new Promise<void>((resolve) => {
      finishDecode = resolve;
    })),
  });

  render(
    <DecodedImage
      src="http://127.0.0.1/fanart.jpg"
      alt="轮播背景"
      onDecoded={onDecoded}
    />,
  );
  const image = screen.getByRole('img', { name: '轮播背景' });

  fireEvent.load(image);
  expect(onDecoded).not.toHaveBeenCalled();

  finishDecode?.();
  await waitFor(() => expect(onDecoded).toHaveBeenCalledTimes(1));
});

test('详情缩略图加载完成后立即显示，但解码回调仍等待真正完成', async () => {
  let finishDecode: (() => void) | undefined;
  const onDecoded = vi.fn();
  Object.defineProperty(HTMLImageElement.prototype, 'decode', {
    configurable: true,
    value: vi.fn(() => new Promise<void>((resolve) => {
      finishDecode = resolve;
    })),
  });

  render(
    <DecodedImage
      src="http://127.0.0.1/episode.jpg"
      alt="剧集缩略图"
      revealOnLoad
      onDecoded={onDecoded}
    />,
  );
  const image = screen.getByRole('img', { name: '剧集缩略图' });

  fireEvent.load(image);
  expect(image).toHaveAttribute('data-image-state', 'ready');
  expect(onDecoded).not.toHaveBeenCalled();

  finishDecode?.();
  await waitFor(() => expect(onDecoded).toHaveBeenCalledTimes(1));
});

test('图片加载失败时保留稳定占位，不暴露浏览器破图或白底', () => {
  render(<DecodedImage src="http://127.0.0.1/missing.jpg" alt="失效海报" />);
  const image = screen.getByRole('img', { name: '失效海报', hidden: true });

  fireEvent.error(image);

  expect(image).toHaveAttribute('data-image-state', 'error');
  expect(image).not.toHaveClass('is-ready');
});

test('缓存命中也等待解码完成，每次状态变化只通知单卡一次', async () => {
  const onStateChange = vi.fn();
  const decode = vi.fn().mockResolvedValue(undefined);
  vi.spyOn(HTMLImageElement.prototype, 'complete', 'get').mockReturnValue(true);
  vi.spyOn(HTMLImageElement.prototype, 'naturalWidth', 'get').mockReturnValue(384);
  Object.defineProperty(HTMLImageElement.prototype, 'decode', { configurable: true, value: decode });

  render(<DecodedImage src="http://127.0.0.1/cached.jpg" alt="缓存海报" onStateChange={onStateChange} />);
  await waitFor(() => expect(screen.getByRole('img')).toHaveAttribute('data-image-state', 'ready'));
  expect(decode).toHaveBeenCalledTimes(1);
  expect(onStateChange.mock.calls.map(([state]) => state)).toEqual(['loading', 'ready']);

  await act(async () => { fireEvent.load(screen.getByRole('img')); });
  expect(onStateChange.mock.calls.map(([state]) => state)).toEqual(['loading', 'ready']);
});

test('地址切换后旧图片的异步解码不得覆盖新图片加载状态', async () => {
  const resolutions: Array<() => void> = [];
  Object.defineProperty(HTMLImageElement.prototype, 'decode', {
    configurable: true,
    value: vi.fn(() => new Promise<void>((resolve) => resolutions.push(resolve))),
  });
  const onStateChange = vi.fn();
  const { rerender } = render(<DecodedImage src="http://127.0.0.1/old.jpg" alt="海报" onStateChange={onStateChange} />);
  fireEvent.load(screen.getByRole('img'));
  rerender(<DecodedImage src="http://127.0.0.1/new.jpg" alt="海报" onStateChange={onStateChange} />);
  fireEvent.load(screen.getByRole('img'));

  await act(async () => { resolutions[0](); });
  expect(screen.getByRole('img')).toHaveAttribute('data-image-state', 'loading');
  expect(onStateChange).not.toHaveBeenCalledWith('ready');

  await act(async () => { resolutions[1](); });
  expect(screen.getByRole('img')).toHaveAttribute('data-image-state', 'ready');
  expect(onStateChange.mock.calls.map(([state]) => state)).toEqual(['loading', 'ready']);
});
