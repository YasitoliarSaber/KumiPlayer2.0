import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/layers.css'
import './styles/foundation.css'
import './index.css'
import './styles/recovery.css'
import App from './App.tsx'
import { initializeDesktopApiSession } from './api/desktopSession'
import AppErrorBoundary from './components/errors/AppErrorBoundary'
import RecoveryView from './components/errors/RecoveryView'

async function bootstrap(): Promise<void> {
  await initializeDesktopApiSession()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <AppErrorBoundary>
        <App />
      </AppErrorBoundary>
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
