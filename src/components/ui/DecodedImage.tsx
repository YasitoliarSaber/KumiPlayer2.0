import { useLayoutEffect, useRef, useState, type ImgHTMLAttributes } from 'react';

type ImageState = 'loading' | 'ready' | 'error';
type DecodedImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  onDecoded?: (image: HTMLImageElement) => void;
};

export default function DecodedImage({
  className = '',
  decoding = 'async',
  onDecoded,
  onError,
  onLoad,
  src,
  ...props
}: DecodedImageProps) {
  const imageRef = useRef<HTMLImageElement | null>(null);
  const generationRef = useRef(0);
  const readyGenerationRef = useRef(0);
  const [imageState, setImageState] = useState<ImageState>(src ? 'loading' : 'error');

  const revealDecodedImage = async (image: HTMLImageElement, generation: number) => {
    try {
      await image.decode();
    } catch {
      // load 已成功时，部分 WebView 仍可能拒绝 decode；继续显示完整资源。
    }
    if (
      generationRef.current === generation
      && imageRef.current === image
      && readyGenerationRef.current !== generation
    ) {
      readyGenerationRef.current = generation;
      setImageState('ready');
      onDecoded?.(image);
    }
  };

  useLayoutEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setImageState(src ? 'loading' : 'error');

    const image = imageRef.current;
    if (src && image?.complete && image.naturalWidth > 0) {
      void revealDecodedImage(image, generation);
    }
  }, [src]);

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
        void revealDecodedImage(event.currentTarget, generationRef.current);
      }}
      onError={(event) => {
        setImageState('error');
        onError?.(event);
      }}
    />
  );
}
