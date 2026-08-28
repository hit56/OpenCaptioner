import type { AuthUser, ScnetAuthConfig } from '../shared/types/auth'

async function parseJsonSafe<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T
  } catch {
    return {} as T
  }
}

function parseErrorDetail(payload: { detail?: unknown }, fallback: string): string {
  const detail = payload.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return fallback
}

export async function fetchScnetAuthConfig(silent = false): Promise<ScnetAuthConfig> {
  const query = silent ? '?silent=1' : ''
  const response = await fetch(`/api/auth/scnet/config${query}`)
  const payload = await parseJsonSafe<ScnetAuthConfig & { detail?: unknown }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '无法获取登录配置'))
  }
  if (!payload.authorize_url) {
    throw new Error('登录配置无效')
  }
  return payload
}

export async function exchangeScnetCode(code: string): Promise<{ user: AuthUser; accessToken?: string }> {
  const response = await fetch('/api/auth/scnet/callback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  const payload = await parseJsonSafe<{ user?: AuthUser; access_token?: string; detail?: unknown }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '超算互联网登录失败'))
  }
  if (!payload.user?.userId) {
    throw new Error('登录响应无效')
  }
  return {
    user: payload.user,
    accessToken: payload.access_token,
  }
}

export async function validateScnetSession(accessToken: string): Promise<{ user: AuthUser }> {
  const response = await fetch('/api/auth/scnet/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ access_token: accessToken }),
  })
  const payload = await parseJsonSafe<{ user?: AuthUser; detail?: unknown }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '登录已失效，请重新登录'))
  }
  if (!payload.user?.userId) {
    throw new Error('登录响应无效')
  }
  return { user: payload.user }
}
