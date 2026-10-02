import { useLayoutEffect, useRef, useState, type ImgHTMLAttributes } from 'react';

export type ImageState = 'loading' | 'ready' | 'error';
type DecodedImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  onDecoded?: (image: HTMLImageElement) => void;
  onStateChange?: (state: ImageState) => void;
  revealOnLoad?: boolean;
};

export default function DecodedImage({
  className = '',
  decoding = 'async',
  onDecoded,
  onStateChange,
  revealOnLoad = false,
  onError,
  onLoad,
  src,
  ...props
}: DecodedImageProps) {
  const imageRef = useRef<HTMLImageElement | null>(null);
  const generationRef = useRef(0);
  const sourceRef = useRef(src);
  const readyGenerationRef = useRef(0);
  const decodedGenerationRef = useRef(0);
  const decodingGenerationRef = useRef(0);
  const [imageState, setImageState] = useState<ImageState>(src ? 'loading' : 'error');

  const revealImage = (image: HTMLImageElement, generation: number) => {
    if (
      generationRef.current === generation
      && imageRef.current === image
      && readyGenerationRef.current !== generation
    ) {
      readyGenerationRef.current = generation;
      setImageState('ready');
    }
  };

  const revealDecodedImage = async (image: HTMLImageElement, generation: number) => {
    // 缓存命中的挂载检查和 load 可能同时到达；每个地址代次只解码一次。
    if (decodingGenerationRef.current === generation) return;
    decodingGenerationRef.current = generation;
    try {
      await image.decode();
    } catch {
      // load 已成功时，部分 WebView 仍可能拒绝 decode；继续显示完整资源。
    }
    if (
      generationRef.current === generation
      && imageRef.current === image
      && decodedGenerationRef.current !== generation
    ) {
      decodedGenerationRef.current = generation;
      revealImage(image, generation);
      onDecoded?.(image);
    }
  };

  useLayoutEffect(() => {
    // 地址变化才开启新代次；StrictMode 重放效果不能重复解码或重置 ready。
    if (generationRef.current === 0 || sourceRef.current !== src) {
      sourceRef.current = src;
      generationRef.current += 1;
      setImageState(src ? 'loading' : 'error');
    }
    const generation = generationRef.current;

    const image = imageRef.current;
    if (src && image?.complete && image.naturalWidth > 0) {
      if (revealOnLoad) revealImage(image, generation);
      void revealDecodedImage(image, generation);
    }
  }, [src, revealOnLoad]);

  useLayoutEffect(() => {
    onStateChange?.(imageState);
  }, [imageState, onStateChange]);

  const stateClassName = imageState === 'ready' ? 'is-ready' : 'is-pending';

  return (
    <img
      {...props}
      ref={imageRef}
      src={src}
      decoding={decoding}
      data-image-state={imageState}
      className={`decoded-image ${stateClassName} ${className}`.trim()}
      onLoad={(event) => {
        onLoad?.(event);
        if (revealOnLoad) revealImage(event.currentTarget, generationRef.current);
        void revealDecodedImage(event.currentTarget, generationRef.current);
      }}
      onError={(event) => {
        setImageState('error');
        onError?.(event);
      }}
    />
  );
}
