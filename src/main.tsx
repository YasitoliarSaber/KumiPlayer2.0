import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RendererProvider } from '@fluentui/react-components'
import { createKumiFluentRenderer } from './design/fluentTheme'
import './styles/layers.css'
import './styles/foundation.css'
import './index.css'
import './styles/recovery.css'
import './styles/task-surfaces.css'
import App from './App.tsx'
import { initializeDesktopApiSession } from './api/desktopSession'
import AppErrorBoundary from './components/errors/AppErrorBoundary'
import RecoveryView from './components/errors/RecoveryView'
import { installDesktopInteractions } from './platform/desktopInteractions'

const uninstallDesktopInteractions = installDesktopInteractions()
import.meta.hot?.dispose(uninstallDesktopInteractions)
const fluentRenderer = createKumiFluentRenderer(document)

async function bootstrap(): Promise<void> {
  await initializeDesktopApiSession()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <RendererProvider renderer={fluentRenderer}>
        <AppErrorBoundary>
          <App />
        </AppErrorBoundary>
      </RendererProvider>
    </StrictMode>,
  )
}

void bootstrap().catch((error: unknown) => {
  const root = document.getElementById('root')
  if (!root) return
  const detail = error instanceof Error ? error.stack || error.message : String(error)
  document.getElementById('kumi-boot-splash')?.remove()
  createRoot(root).render(
    <RecoveryView
      title="启动失败"
      message="桌面安全会话或本地后端未能就绪。"
      detail={detail}
    />,
  )
})
