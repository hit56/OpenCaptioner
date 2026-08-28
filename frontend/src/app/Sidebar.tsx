import { useCallback, useEffect, useState } from 'react'
import { displayUserName, useAuth } from '../features/auth/AuthProvider'
import { AdminStatsPanel } from '../features/admin/AdminStatsPanel'
import { useI18n } from '../shared/i18n/useI18n'
import { BrandLogo } from '../shared/ui/BrandLogo'
import { SidebarToggleIcon } from '../shared/ui/SidebarToggleIcon'
import { TabIcon } from '../shared/ui/TabIcons'
import { useAppState } from './AppState'

const MOBILE_QUERY = '(max-width: 900px)'

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia(MOBILE_QUERY).matches : false,
  )

  useEffect(() => {
    const media = window.matchMedia(MOBILE_QUERY)
    const onChange = (event: MediaQueryListEvent) => setIsMobile(event.matches)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  return isMobile
}

export function Sidebar() {
  const { store, dispatch } = useAppState()
  const { session, logout } = useAuth()
  const { t, lang, setLang } = useI18n()
  const isMobile = useIsMobile()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [adminPanelOpen, setAdminPanelOpen] = useState(false)

  const sidebarOpen = store.ui.sidebarOpen
  const isAdmin = Boolean(session?.user.isAdmin)

  const closeMobile = useCallback(() => setMobileOpen(false), [])

  function handleUploadNavClick() {
    dispatch({ type: 'toggle-upload-history' })
    closeMobile()
  }

  function toggleLang() {
    setLang(lang === 'zh-CN' ? 'en' : 'zh-CN')
  }

  function toggleSidebar() {
    dispatch({ type: 'toggle-sidebar-open' })
  }

  const sidebarClassName = [
    'sidebar',
    mobileOpen && 'active',
    !isMobile && sidebarOpen && 'sidebar--open',
    !isMobile && !sidebarOpen && 'sidebar--hidden',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <>
      {!isMobile && !sidebarOpen ? (
        <button
          type="button"
          className="sidebar-toggle-btn"
          data-click-action="sidebar_toggle"
          data-click-label="展开/收起侧边栏"
          aria-expanded={sidebarOpen}
          aria-controls="app-sidebar"
          title={sidebarOpen ? t('sidebarHide') : t('sidebarShow')}
          onClick={toggleSidebar}
        >
          <SidebarToggleIcon />
        </button>
      ) : null}

      <button
        type="button"
        className={`mobile-menu-btn${mobileOpen ? ' active' : ''}`}
        data-click-action="mobile_menu"
        data-click-label="移动端菜单"
        aria-label="Menu"
        onClick={() => setMobileOpen((value) => !value)}
      >
        <span />
        <span />
        <span />
      </button>

      <div
        className={`sidebar-overlay${mobileOpen ? ' active' : ''}`}
        role="presentation"
        onClick={closeMobile}
      />

      <aside id="app-sidebar" className={sidebarClassName}>
        <div className="sidebar-header">
          {!isMobile ? (
            <button
              type="button"
              className="sidebar-header-btn sidebar-header-toggle"
              data-click-action="sidebar_toggle"
              data-click-label="展开/收起侧边栏"
              aria-expanded={sidebarOpen}
              aria-controls="app-sidebar"
              title={sidebarOpen ? t('sidebarHide') : t('sidebarShow')}
              onClick={toggleSidebar}
            >
              <SidebarToggleIcon />
            </button>
          ) : (
            <span className="sidebar-header-btn sidebar-header-btn--spacer" aria-hidden />
          )}
          <div className="sidebar-title-block">
            <BrandLogo variant="compact" className="sidebar-brand-logo" />
            {session ? (
              <div className="sidebar-header-user">
                <span className="sidebar-header-user-status">{t('loggedInStatus')}</span>
                <span
                  className="sidebar-header-user-name"
                  title={session.user.userName || displayUserName(session)}
                >
                  {session.user.userName || displayUserName(session)}
                </span>
              </div>
            ) : null}
          </div>
          <button
            type="button"
            className="sidebar-header-btn lang-globe-btn"
            data-click-action="lang_toggle"
            data-click-label="切换语言"
            title={t('language')}
            onClick={toggleLang}
          >
            <svg viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="2" y1="12" x2="22" y2="12" />
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
            </svg>
          </button>
        </div>

        <nav className="tab-nav">
          <div className="tab-nav-group">
            <button
              type="button"
              className="tab-btn active"
              data-click-action="tab_upload"
              data-click-label={t('tabUpload')}
              data-click-tab="upload"
              onClick={handleUploadNavClick}
              title={t('tabUpload')}
              aria-expanded={store.ui.uploadHistoryExpanded}
            >
              <TabIcon name="upload" />
              <span className="tab-label">{t('tabUpload')}</span>
            </button>
            <div
              id="sidebar-upload-history-slot"
              className={`sidebar-upload-history${
                store.ui.uploadHistoryExpanded ? ' expanded' : ''
              }`}
              aria-hidden={!store.ui.uploadHistoryExpanded}
            />
          </div>
          {isAdmin ? (
            <div className="tab-nav-group">
              <button
                type="button"
                className="tab-btn"
                data-click-action="tab_admin_stats"
                data-click-label={t('adminStats')}
                data-click-tab="admin"
                onClick={() => {
                  setAdminPanelOpen(true)
                  closeMobile()
                }}
                title={t('adminStats')}
              >
                <TabIcon name="stats" />
                <span className="tab-label">{t('adminStats')}</span>
              </button>
            </div>
          ) : null}
        </nav>

        {session ? (
          <div className="sidebar-footer">
            <button
              type="button"
              className="sidebar-logout-btn"
              data-click-action="logout"
              data-click-label="退出登录"
              onClick={logout}
            >
              {t('logout')}
            </button>
          </div>
        ) : null}
      </aside>

      {isAdmin ? (
        <AdminStatsPanel open={adminPanelOpen} onClose={() => setAdminPanelOpen(false)} />
      ) : null}
    </>
  )
}
