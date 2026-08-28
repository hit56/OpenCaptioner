import { useEffect, useState } from 'react'
import { useI18n } from '../../shared/i18n/useI18n'
import { fetchScnetAuthConfig } from '../../services/authApi'
import { useAuth } from './AuthProvider'

export function LoginPage() {
  const { t, lang, setLang } = useI18n()
  const { oauthError, clearOauthError, setAuthView } = useAuth()
  const [formError, setFormError] = useState<string | null>(null)
  const [scnetLoading, setScnetLoading] = useState(false)
  const [scnetConfigError, setScnetConfigError] = useState<string | null>(null)

  useEffect(() => {
    if (!oauthError) return
    setFormError(oauthError)
  }, [oauthError])

  async function handleScnetLogin() {
    clearOauthError()
    setFormError(null)
    setScnetConfigError(null)
    setScnetLoading(true)
    try {
      const config = await fetchScnetAuthConfig(false)
      window.location.assign(config.authorize_url)
    } catch (error) {
      const message = error instanceof Error ? error.message : t('loginScnetFailed')
      setScnetConfigError(message)
      setScnetLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-page-toolbar">
        <button
          type="button"
          className="login-link-btn"
          onClick={() => {
            clearOauthError()
            setAuthView('home')
          }}
        >
          {t('homeBack')}
        </button>
        <label className="home-lang-select login-lang-select">
          <span className="home-lang-label">{t('language')}</span>
          <select
            value={lang}
            aria-label={t('language')}
            onChange={(event) => setLang(event.target.value as 'zh-CN' | 'en')}
          >
            <option value="zh-CN">中文</option>
            <option value="en">English</option>
          </select>
        </label>
      </div>
      <div className="login-card">
        <h1 className="login-title">{t('loginTitle')}</h1>

        {formError ? <p className="login-error">{formError}</p> : null}
        {scnetConfigError ? <p className="login-error">{scnetConfigError}</p> : null}

        <button
          type="button"
          className="login-scnet-btn"
          onClick={() => void handleScnetLogin()}
          disabled={scnetLoading}
        >
          <span className="login-scnet-logo">SCNet</span>
          <span>{t('loginScnet')}</span>
        </button>
      </div>
    </div>
  )
}
