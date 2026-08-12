import { useEffect, type RefObject } from 'react';

/** 让临时操作菜单在点击外部或按 Esc 时自动收起。 */
export function useDismissiblePopover(
  open: boolean,
  onDismiss: () => void,
  containerRef: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) onDismiss();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onDismiss();
    };

    document.addEventListener('pointerdown', handlePointerDown, true);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown, true);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [containerRef, onDismiss, open]);
}
