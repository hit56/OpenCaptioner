import type { AuthSession } from '../../shared/types/auth'

const AUTH_SESSION_KEY = 'asr_auth_session'

export function readAuthSession(): AuthSession | null {
  try {
    const raw = localStorage.getItem(AUTH_SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as AuthSession
    if (!parsed?.user?.userId) return null
    return parsed
  } catch {
    localStorage.removeItem(AUTH_SESSION_KEY)
    return null
  }
}

export function writeAuthSession(session: AuthSession): void {
  localStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session))
}

export function clearAuthSession(): void {
  localStorage.removeItem(AUTH_SESSION_KEY)
}

export function getAuthenticatedUserId(): string | null {
  return readAuthSession()?.user.userId ?? null
}
