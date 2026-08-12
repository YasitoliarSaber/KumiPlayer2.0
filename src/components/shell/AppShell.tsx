import { type CSSProperties, type ReactNode, useRef } from 'react';
import Sidebar from './Sidebar';
import DesktopTitleBar from './DesktopTitleBar';
import ScrollProgressButton from './ScrollProgressButton';
import { SIDEBAR_WIDTHS, useUiStore } from '../../stores/ui';

interface AppShellProps {
  children: ReactNode;
}

export default function AppShell({ children }: AppShellProps) {
  const { sidebarMode } = useUiStore();
  const mainRef = useRef<HTMLElement>(null);

  return (
    <div
      className={`app-shell sidebar-${sidebarMode} flex min-h-screen`}
      style={{
        background: 'var(--app-bg)',
        '--sidebar-width': `${SIDEBAR_WIDTHS[sidebarMode]}px`,
      } as CSSProperties}
    >
      <DesktopTitleBar />
      <Sidebar />
      <main ref={mainRef} className="app-main flex-1">
        <div className="app-content px-4 pb-5 2xl:px-6">{children}</div>
        <ScrollProgressButton scrollContainerRef={mainRef} />
      </main>
    </div>
  );
}
