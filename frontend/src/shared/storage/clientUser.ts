import { getAuthenticatedUserId } from '../../features/auth/authStorage'

const CLIENT_USER_KEY = 'asr_client_user_id'

export function getOrCreateClientUserId(): string {
  const authenticatedUserId = getAuthenticatedUserId()
  if (authenticatedUserId) return `scnet:${authenticatedUserId}`

  try {
    const existing = localStorage.getItem(CLIENT_USER_KEY)
    if (existing) return existing
    const id = crypto.randomUUID()
    localStorage.setItem(CLIENT_USER_KEY, id)
    return id
  } catch {
    return 'anonymous'
  }
}

/** 与网关 uploads 文件名中的 user 段一致：匿名 ID 去掉连字符后的前 8 位 hex */
export function clientUserIdTag(userId = getOrCreateClientUserId()): string {
  if (!userId || userId === 'anonymous') return ''
  return userId.replace(/-/g, '').toLowerCase().slice(0, 8)
}

export function userScopedKey(base: string): string {
  return `${base}:${getOrCreateClientUserId()}`
}

export function appendClientUserQuery(url: string): string {
  if (!url || url.startsWith('blob:')) return url
  const userId = getOrCreateClientUserId()
  if (!userId || userId === 'anonymous') return url
  try {
    const parsed = new URL(url, window.location.origin)
    parsed.searchParams.set('client_user_id', userId)
    if (url.startsWith('http://') || url.startsWith('https://')) {
      return parsed.toString()
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    const sep = url.includes('?') ? '&' : '?'
    return `${url}${sep}client_user_id=${encodeURIComponent(userId)}`
  }
}
