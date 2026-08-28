export interface AuthUser {
  userId: string
  userName?: string
  fullName?: string
  email?: string
  mobile?: string
  isAdmin?: boolean
}

export type AuthLoginMethod = 'scnet' | 'password'

export interface AuthSession {
  user: AuthUser
  loginMethod: AuthLoginMethod
  remember: boolean
  loggedInAt: number
  accessToken?: string
}

export const SCNET_OAUTH_SILENT_STATE = 'silent_sso'

export interface ScnetAuthConfig {
  authorize_url: string
  client_id: string
}
