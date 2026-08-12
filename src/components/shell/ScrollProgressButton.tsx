import { ArrowUp } from 'lucide-react';
import { type RefObject, useEffect, useRef } from 'react';

interface ScrollProgressButtonProps {
  scrollContainerRef: RefObject<HTMLElement | null>;
}

export default function ScrollProgressButton({ scrollContainerRef }: ScrollProgressButtonProps) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const ringRef = useRef<SVGCircleElement>(null);
  const statusRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const scrollContainer = scrollContainerRef.current;
    const button = buttonRef.current;
    const ring = ringRef.current;
    const status = statusRef.current;
    if (!scrollContainer || !button || !ring || !status) return undefined;

    let animationFrame = 0;
    const updateProgress = () => {
      animationFrame = 0;
      const { scrollTop, scrollHeight, clientHeight } = scrollContainer;
      const maxScroll = Math.max(0, scrollHeight - clientHeight);
      const nextProgress = maxScroll > 0 ? Math.min(100, Math.max(0, (scrollTop / maxScroll) * 100)) : 0;
      const roundedProgress = Math.round(nextProgress);
      const visible = maxScroll > 0;

      ring.style.strokeDashoffset = String(100 - nextProgress);
      button.classList.toggle('is-visible', visible);
      button.setAttribute('aria-hidden', String(!visible));
      button.setAttribute('aria-label', `返回顶部，当前滚动进度 ${roundedProgress}%`);
      button.tabIndex = visible ? 0 : -1;
      status.setAttribute('aria-valuenow', String(roundedProgress));
      status.setAttribute('aria-valuetext', `${roundedProgress}%`);
    };
    const scheduleUpdate = () => {
      if (!animationFrame) animationFrame = window.requestAnimationFrame(updateProgress);
    };

    updateProgress();
    scrollContainer.addEventListener('scroll', scheduleUpdate, { passive: true });
    window.addEventListener('resize', scheduleUpdate);

    const resizeObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(scheduleUpdate) : null;
    resizeObserver?.observe(scrollContainer);
    const content = scrollContainer.querySelector<HTMLElement>('.app-content');
    if (content) resizeObserver?.observe(content);

    return () => {
      scrollContainer.removeEventListener('scroll', scheduleUpdate);
      window.removeEventListener('resize', scheduleUpdate);
      resizeObserver?.disconnect();
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
    };
  }, [scrollContainerRef]);

  const returnToTop = () => {
    const reduceMotion = document.documentElement.dataset.motion === 'reduced'
      || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    scrollContainerRef.current?.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
  };

  return (
    <button
      ref={buttonRef}
      type="button"
      className="scroll-progress-button"
      onClick={returnToTop}
      aria-label="返回顶部，当前滚动进度 0%"
      aria-hidden="true"
      tabIndex={-1}
      title="返回顶部"
    >
      <span
        ref={statusRef}
        className="scroll-progress-status"
        role="progressbar"
        aria-label="当前页滚动进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={0}
        aria-valuetext="0%"
      />
      <svg className="scroll-progress-ring" viewBox="0 0 44 44" aria-hidden="true">
        <circle className="scroll-progress-ring-track" cx="22" cy="22" r="18" pathLength="100" />
        <circle
          ref={ringRef}
          className="scroll-progress-ring-value"
          cx="22"
          cy="22"
          r="18"
          pathLength="100"
          strokeDasharray="100"
          strokeDashoffset="100"
        />
      </svg>
      <ArrowUp size={18} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}
