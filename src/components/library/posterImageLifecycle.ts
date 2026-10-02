import { createContext } from 'react';
import { api } from '../../api/client';

interface ImageEntry {
  path: string;
  width: number;
  url: string;
  local: boolean;
  active: boolean;
  ready: boolean;
}

// 一个网格共用一个观察器和批量状态查询，滚动不进入 React 更新队列。
export class PosterImageLifecycle {
  private entries = new Map<HTMLImageElement, ImageEntry>();
  private observer: IntersectionObserver | null = null;
  private timer = 0;
  private request: AbortController | null = null;
  private disposed = false;

  configure(root: HTMLElement | null, height: number) {
    this.disposed = false;
    this.observer?.disconnect();
    if (typeof IntersectionObserver === 'undefined') return;
    this.observer = new IntersectionObserver((changes) => {
      for (const change of changes) {
        const image = change.target as HTMLImageElement;
        const entry = this.entries.get(image);
        if (!entry) continue;
        entry.active = change.isIntersecting;
        if (entry.active) this.activate(image, entry);
        else {
          image.removeAttribute('src');
          image.dataset.state = 'idle';
        }
      }
    }, { root, rootMargin: `${Math.round(height * 1.25)}px 0px` });
    for (const image of this.entries.keys()) this.observer.observe(image);
  }

  register(image: HTMLImageElement, entry: Omit<ImageEntry, 'active' | 'ready'>) {
    const record = { ...entry, active: false, ready: false };
    this.entries.set(image, record);
    if (this.observer) this.observer.observe(image);
    else {
      // 无观察器环境由浏览器原生懒加载兜底。
      image.loading = 'lazy';
      record.active = true;
      this.activate(image, record);
    }
    return () => {
      this.observer?.unobserve(image);
      this.entries.delete(image);
      image.removeAttribute('src');
    };
  }

  private activate(image: HTMLImageElement, entry: ImageEntry) {
    if (image.hasAttribute('src')) return;
    image.dataset.state = 'loading';
    if (!entry.local || entry.ready) image.src = entry.url;
    else {
      // 新进入视野的冷图立即查询，不能等待旧轮询的静默间隔。
      window.clearTimeout(this.timer);
      this.timer = 0;
      this.schedule(0);
    }
  }

  private schedule(delay: number) {
    if (this.disposed || this.timer || this.request) return;
    this.timer = window.setTimeout(() => {
      this.timer = 0;
      void this.poll();
    }, delay);
  }

  private async poll() {
    const pending = [...this.entries].filter(([image, entry]) =>
      entry.active && entry.local && !image.hasAttribute('src')
        && (image.dataset.state === 'loading' || image.dataset.state === 'retry'));
    if (!pending.length || this.disposed) return;
    const controller = new AbortController();
    this.request = controller;
    let delay = 750;
    try {
      for (let start = 0; start < pending.length; start += 64) {
        const batch = pending.slice(start, start + 64);
        const response = await api.post<{ states: string[] }>('/api/assets/thumbnails/prepare', {
          items: batch.map(([, entry]) => ({ path: entry.path, width: entry.width })),
        }, { signal: controller.signal });
        if (this.disposed || controller.signal.aborted) return;
        if (response.states.every((state) => state === 'retry')) delay = 2000;
        batch.forEach(([image, entry], index) => {
          if (this.entries.get(image) !== entry) return;
          if (response.states[index] === 'ready') entry.ready = true;
          if (!entry.active) return;
          if (entry.ready) image.src = entry.url;
          else if (response.states[index] === 'retry') image.dataset.state = 'retry';
          else if (response.states[index] === 'unavailable') image.dataset.state = 'error';
        });
      }
    } catch {
      delay = 2000;
    } finally {
      if (this.request === controller) this.request = null;
      if (!this.disposed) this.schedule(delay);
    }
  }

  dispose() {
    this.disposed = true;
    this.observer?.disconnect();
    this.observer = null;
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.request?.abort();
    this.request = null;
  }
}

export const PosterImageContext = createContext<PosterImageLifecycle | null>(null);
