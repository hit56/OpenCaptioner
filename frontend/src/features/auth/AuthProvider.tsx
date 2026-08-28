import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { AuthSession, AuthUser } from '../../shared/types/auth'
import { SCNET_OAUTH_SILENT_STATE } from '../../shared/types/auth'
import { exchangeScnetCode, fetchScnetAuthConfig, validateScnetSession } from '../../services/authApi'
import {
  clearAuthSession,
  readAuthSession,
  writeAuthSession,
} from './authStorage'

export type AuthView = 'home' | 'login'

interface AuthContextValue {
  session: AuthSession | null
  isAuthenticated: boolean
  isBootstrapping: boolean
  oauthError: string | null
  authView: AuthView
  setAuthView: (view: AuthView) => void
  loginWithScnetUser: (user: AuthUser, remember?: boolean, accessToken?: string) => void
  logout: () => void
  clearOauthError: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const SILENT_SSO_ERRORS = new Set(['login_required', 'interaction_required', 'consent_required'])
// Guard so we only attempt silent SSO once per browser session, avoiding a redirect loop
// when the user is not logged into SCNet.
const SILENT_SSO_ATTEMPTED_KEY = 'asr_silent_sso_attempted'

function stripOAuthQueryParams(): void {
  const url = new URL(window.location.href)
  if (
    !url.searchParams.has('code') &&
    !url.searchParams.has('source') &&
    !url.searchParams.has('error')
  ) {
    return
  }
  url.searchParams.delete('code')
  url.searchParams.delete('source')
  url.searchParams.delete('state')
  url.searchParams.delete('error')
  url.searchParams.delete('error_description')
  const next = `${url.pathname}${url.search}${url.hash}`
  window.history.replaceState({}, document.title, next || '/')
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(() => readAuthSession())
  const [isBootstrapping, setIsBootstrapping] = useState(true)
  const [oauthError, setOauthError] = useState<string | null>(null)
  const [authView, setAuthView] = useState<AuthView>('home')

  const loginWithScnetUser = useCallback((user: AuthUser, remember = true, accessToken?: string) => {
    const nextSession: AuthSession = {
      user,
      loginMethod: 'scnet',
      remember,
      loggedInAt: Date.now(),
      accessToken: accessToken || undefined,
    }
    writeAuthSession(nextSession)
    setSession(nextSession)
    setOauthError(null)
  }, [])

  const logout = useCallback(() => {
    clearAuthSession()
    setSession(null)
    setOauthError(null)
    setAuthView('home')
  }, [])

  const clearOauthError = useCallback(() => setOauthError(null), [])

  useEffect(() => {
    let cancelled = false

    async function bootstrapAuth() {
      const params = new URLSearchParams(window.location.search)
      const code = params.get('code')?.trim()
      const oauthErrorParam = params.get('error')?.trim()
      const oauthState = params.get('state')?.trim()

      if (oauthErrorParam && !code) {
        // Ignore expected silent-SSO failures (e.g. user not logged into SCNet); show other errors.
        const ignoreError =
          oauthState === SCNET_OAUTH_SILENT_STATE || SILENT_SSO_ERRORS.has(oauthErrorParam)
        if (!ignoreError) {
          const description = params.get('error_description')?.trim()
          if (!cancelled) {
            setOauthError(description || '超算互联网登录失败')
          }
        }
        stripOAuthQueryParams()
        if (!cancelled) setIsBootstrapping(false)
        return
      }

      if (code) {
        try {
          const { user, accessToken } = await exchangeScnetCode(code)
          if (cancelled) return
          loginWithScnetUser(user, true, accessToken)
        } catch (error) {
          if (cancelled) return
          const message = error instanceof Error ? error.message : '超算互联网登录失败'
          setOauthError(message)
        } finally {
          if (!cancelled) {
            stripOAuthQueryParams()
            setIsBootstrapping(false)
          }
        }
        return
      }

      const existing = readAuthSession()
      if (existing?.accessToken) {
        try {
          const { user } = await validateScnetSession(existing.accessToken)
          if (cancelled) return
          loginWithScnetUser(user, existing.remember, existing.accessToken)
          setIsBootstrapping(false)
          return
        } catch {
          if (cancelled) return
          clearAuthSession()
          setSession(null)
        }
      } else if (existing?.user?.userId) {
        if (!cancelled) setIsBootstrapping(false)
        return
      }

      // No active session: silently probe SCNet to detect an already-logged-in user.
      // Only attempt once per browser session to avoid a redirect loop when the user
      // is not logged into SCNet. On success SCNet redirects back with ?code=; on
      // failure (login_required) it redirects back with ?error=, which we ignore.
      const alreadyAttempted = sessionStorage.getItem(SILENT_SSO_ATTEMPTED_KEY)
      if (!alreadyAttempted) {
        sessionStorage.setItem(SILENT_SSO_ATTEMPTED_KEY, '1')
        try {
          const config = await fetchScnetAuthConfig(true)
          if (!cancelled) {
            window.location.assign(config.authorize_url)
          }
          return
        } catch {
          // Fall through to show the landing page if the config request fails.
          sessionStorage.removeItem(SILENT_SSO_ATTEMPTED_KEY)
        }
      }

      if (!cancelled) setIsBootstrapping(false)
    }

    void bootstrapAuth()
    return () => {
      cancelled = true
    }
  }, [loginWithScnetUser])

  const value = useMemo(
    () => ({
      session,
      isAuthenticated: session !== null,
      isBootstrapping,
      oauthError,
      authView,
      setAuthView,
      loginWithScnetUser,
      logout,
      clearOauthError,
    }),
    [
      session,
      isBootstrapping,
      oauthError,
      authView,
      loginWithScnetUser,
      logout,
      clearOauthError,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

export function displayUserName(session: AuthSession | null): string {
  if (!session) return ''
  const { user } = session
  return user.fullName || user.userName || user.mobile || user.email || user.userId
}
