import { useContext, useEffect, useRef } from 'react';
import { PosterImageContext } from './posterImageLifecycle';

export default function PosterImage({ path, width, src, alt, local }: {
  path: string; width: number; src: string; alt: string; local: boolean;
}) {
  const ref = useRef<HTMLImageElement>(null);
  const lifecycle = useContext(PosterImageContext);
  useEffect(() => {
    const image = ref.current;
    if (!image || !lifecycle) return;
    image.dataset.state = 'idle';
    return lifecycle.register(image, { path, width, url: src, local });
  }, [lifecycle, path, width, src]);
  return <img ref={ref} alt={alt} className="poster-image poster-managed-image" decoding="async"
    onLoad={(event) => { if (event.currentTarget.hasAttribute('src')) event.currentTarget.dataset.state = 'ready'; }}
    onError={(event) => { if (event.currentTarget.hasAttribute('src')) event.currentTarget.dataset.state = 'error'; }} />;
}
