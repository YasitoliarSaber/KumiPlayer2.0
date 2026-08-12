import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

test('图片加载失败时保留稳定占位，不暴露浏览器破图或白底', () => {
  render(<DecodedImage src="http://127.0.0.1/missing.jpg" alt="失效海报" />);
  const image = screen.getByRole('img', { name: '失效海报', hidden: true });

  fireEvent.error(image);

  expect(image).toHaveAttribute('data-image-state', 'error');
  expect(image).not.toHaveClass('is-ready');
});
