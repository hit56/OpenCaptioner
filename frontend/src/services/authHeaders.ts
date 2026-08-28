import { readAuthSession } from '../features/auth/authStorage'

export function getAccessToken(): string | null {
  const token = readAuthSession()?.accessToken
  return token?.trim() ? token.trim() : null
}

export function authHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  const token = getAccessToken()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  return headers
}
