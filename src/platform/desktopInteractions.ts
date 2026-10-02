/** 页面保留业务右键菜单，输入区保留系统编辑菜单。 */
export function installDesktopInteractions(): () => void {
  const handleContextMenu = (event: MouseEvent) => {
    if (event.defaultPrevented) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, [contenteditable]:not([contenteditable="false"])')) return;
    event.preventDefault();
  };

  // 冒泡阶段执行，React 剧集菜单先收到事件，不影响已有业务操作。
  document.addEventListener('contextmenu', handleContextMenu);
  return () => document.removeEventListener('contextmenu', handleContextMenu);
}
