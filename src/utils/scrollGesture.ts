// 共享的滚动静默状态（单例）。
// 整个应用只注册一个 window scroll 监听器，避免每张海报卡各自监听滚动，
// 把滚动事件放大为 O(卡片数) 的 JS 与计时器操作。

const SCROLL_QUIET_MS = 300;

let scrollRecentlyActive = false;
let resetTimer: number | null = null;
let listenerInstalled = false;

function markScrolling(): void {
  scrollRecentlyActive = true;
  if (resetTimer !== null) window.clearTimeout(resetTimer);
  resetTimer = window.setTimeout(() => {
    scrollRecentlyActive = false;
    resetTimer = null;
  }, SCROLL_QUIET_MS);
}

function installScrollListener(): void {
  if (listenerInstalled || typeof window === 'undefined') return;
  listenerInstalled = true;
  window.addEventListener('scroll', markScrolling, { capture: true, passive: true });
}

/** 最近 300ms 内是否发生过滚动；首次调用会安装唯一的共享监听器。 */
export function isScrollRecentlyActive(): boolean {
  installScrollListener();
  return scrollRecentlyActive;
}

/** 仅供测试重置单例状态，避免用例间污染。 */
export function resetScrollGestureForTests(): void {
  if (typeof window !== 'undefined' && listenerInstalled) {
    window.removeEventListener('scroll', markScrolling, { capture: true });
  }
  listenerInstalled = false;
  if (resetTimer !== null) {
    window.clearTimeout(resetTimer);
    resetTimer = null;
  }
  scrollRecentlyActive = false;
}
