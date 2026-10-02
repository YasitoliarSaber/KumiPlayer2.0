import { afterEach, expect, test, vi } from 'vitest';
import { installDesktopInteractions } from '../../src/platform/desktopInteractions';

const uninstallers: Array<() => void> = [];
afterEach(() => { uninstallers.splice(0).forEach((uninstall) => uninstall()); document.body.innerHTML = ''; });

test('空白处和海报不显示浏览器右键菜单', () => {
  uninstallers.push(installDesktopInteractions());
  document.body.innerHTML = '<button><img alt="海报"></button>';
  for (const target of [document.body, document.querySelector('img')!]) {
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  }
});

test('输入框与可编辑区域保留原生复制粘贴菜单', () => {
  uninstallers.push(installDesktopInteractions());
  document.body.innerHTML = '<input><textarea></textarea><div contenteditable="true"><span>文字</span></div>';
  for (const target of document.querySelectorAll('input,textarea,span')) {
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  }
});

test('剧集业务菜单仍能处理右键，卸载后移除监听', () => {
  const uninstall = installDesktopInteractions();
  const handler = vi.fn((event: Event) => event.preventDefault());
  const episode = document.createElement('button');
  document.body.append(episode);
  episode.addEventListener('contextmenu', handler);
  episode.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
  expect(handler).toHaveBeenCalledTimes(1);
  uninstall();
  const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
  document.body.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
});
