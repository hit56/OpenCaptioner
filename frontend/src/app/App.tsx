import { ClickLogRegistrar } from '../shared/analytics/ClickLogRegistrar'
import { AuthProvider, useAuth } from '../features/auth/AuthProvider'
import { HomePage } from '../features/auth/HomePage'
import { UploadTab } from '../features/upload/UploadTab'
import { useI18n } from '../shared/i18n/useI18n'
import { I18nProvider } from '../shared/i18n/I18nProvider'
import { AppStateProvider, useAppState } from './AppState'
import { Sidebar } from './Sidebar'

function MainApp() {
  const { store } = useAppState()
  const { isAuthenticated, isBootstrapping } = useAuth()
  const { t } = useI18n()

  if (isBootstrapping) {
    return <div className="login-bootstrapping">{t('loginDetecting')}</div>
  }

  if (!isAuthenticated) {
    return <HomePage />
  }

  return (
    <>
      <ClickLogRegistrar />
      <div className={`page-shell${store.ui.sidebarOpen ? '' : ' sidebar-collapsed'}`}>
        <Sidebar />
        <main className="main-content">
          <div id="tab-upload" className="tab-content active">
            <UploadTab />
          </div>
        </main>
      </div>
    </>
  )
}

export function App() {
  return (
    <I18nProvider>
      <AuthProvider>
        <AppStateProvider>
          <MainApp />
        </AppStateProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
