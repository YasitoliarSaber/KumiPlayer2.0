import { type CSSProperties, type ReactNode } from 'react';
import Sidebar from './Sidebar';
import DesktopTitleBar from './DesktopTitleBar';
import { SIDEBAR_WIDTHS, useUiStore } from '../../stores/ui';

interface AppShellProps {
  children: ReactNode;
}

export default function AppShell({ children }: AppShellProps) {
  const sidebarMode = useUiStore((state) => state.sidebarMode);

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
      <main className="app-main flex-1">
        <div className="app-content px-4 pb-5 2xl:px-6">{children}</div>
      </main>
    </div>
  );
}
