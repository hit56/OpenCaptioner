import { useEffect, useState } from 'react'
import heroBg from '../../assets/home-hero-bg.png'
import { fetchScnetAuthConfig } from '../../services/authApi'
import { useI18n } from '../../shared/i18n/useI18n'
import type { LangCode } from '../../shared/types/asr'
import { BrandLogo } from '../../shared/ui/BrandLogo'
import { useAuth } from './AuthProvider'

const LANG_OPTIONS: { value: LangCode; label: string }[] = [
  { value: 'zh-CN', label: '中文' },
  { value: 'en', label: 'English' },
]

const FEATURES = [
  { key: 'homeFeatAv', icon: 'media' },
  { key: 'homeFeatSpeaker', icon: 'users' },
  { key: 'homeFeatSearch', icon: 'search' },
  { key: 'homeFeatClip', icon: 'clip' },
] as const

function FeatureIcon({ name }: { name: string }) {
  const common = {
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true as const,
  }

  switch (name) {
    case 'users':
      return (
        <svg {...common}>
          <circle cx="9" cy="8" r="3" />
          <circle cx="17" cy="9" r="2.5" />
          <path d="M3 18c0-2.5 2.5-4 6-4s6 1.5 6 4" />
          <path d="M14.5 14.2c1.7.4 3.5 1.3 3.5 3.3" />
        </svg>
      )
    case 'search':
      return (
        <svg {...common}>
          <circle cx="11" cy="11" r="6.5" />
          <path d="m16 16 4 4" />
        </svg>
      )
    case 'clip':
      return (
        <svg {...common}>
          <path d="M8 5v14l11-7L8 5Z" />
        </svg>
      )
    case 'media':
      return (
        <svg {...common}>
          <rect x="3" y="6" width="18" height="12" rx="2" />
          <path d="M10 10v4l4-2-4-2Z" />
        </svg>
      )
    default:
      return (
        <svg {...common}>
          <path d="M5 12.5 10 17l9-10" />
        </svg>
      )
  }
}

export function HomePage() {
  const { t, lang, setLang } = useI18n()
  const { oauthError, clearOauthError } = useAuth()
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
    <div className="home-page">
      <header className="home-topbar">
        <div className="home-topbar-brand">
          <BrandLogo variant="mark" className="home-topbar-logo" />
          <div className="home-topbar-brand-copy">
            <span className="home-topbar-brand-text">{t('homeBrand')}</span>
            <span className="home-topbar-brand-sub">{t('homeBrandEn')}</span>
          </div>
        </div>
        <label className="home-lang-select">
          <span className="home-lang-label">{t('language')}</span>
          <select
            value={lang}
            aria-label={t('language')}
            onChange={(event) => setLang(event.target.value as LangCode)}
          >
            {LANG_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </header>

      <div className="home-split">
        <section className="home-hero-panel" aria-label={t('homeHeadline')}>
          <div className="home-hero-bg" aria-hidden>
            <img className="home-hero-bg-image" src={heroBg} alt="" draggable={false} />
            <div className="home-hero-bg-shade" />
          </div>

          <div className="home-hero-content">
            <h1 className="home-hero-title">{t('homeHeadline')}</h1>
            <p className="home-hero-subtitle">{t('homeSubtitle')}</p>

            <ul className="home-feature-row">
              {FEATURES.map((feature) => (
                <li key={feature.key} className="home-feature-item">
                  <span className="home-feature-icon">
                    <FeatureIcon name={feature.icon} />
                  </span>
                  <span className="home-feature-text">{t(feature.key)}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="home-auth-panel" aria-label={t('loginAuthTitle')}>
          <div className="home-auth-card">
            <h2 className="home-auth-title">{t('loginAuthTitle')}</h2>
            <p className="home-auth-subtitle">{t('loginAuthSubtitle')}</p>

            <div className="home-auth-divider" aria-hidden />

            <p className="home-auth-hint">{t('loginAuthHint')}</p>

            {formError ? <p className="login-error">{formError}</p> : null}
            {scnetConfigError ? <p className="login-error">{scnetConfigError}</p> : null}

            <button
              type="button"
              className="home-scnet-btn"
              data-click-action="home_scnet_login"
              data-click-label={t('loginScnetCta')}
              onClick={() => void handleScnetLogin()}
              disabled={scnetLoading}
            >
              <span className="home-scnet-badge" aria-hidden>
                SC
              </span>
              <span>{scnetLoading ? t('loginProcessing') : t('loginScnetCta')}</span>
            </button>

            <p className="home-auth-footnote">{t('loginAuthFootnote')}</p>
          </div>
        </section>
      </div>
    </div>
  )
}
