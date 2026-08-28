import { userScopedKey } from './clientUser'

export function readUserJson<T>(baseKey: string): T | null {
  try {
    const raw = localStorage.getItem(userScopedKey(baseKey))
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    localStorage.removeItem(userScopedKey(baseKey))
    return null
  }
}

export function writeUserJson(baseKey: string, value: unknown): void {
  try {
    localStorage.setItem(userScopedKey(baseKey), JSON.stringify(value))
  } catch {
    // localStorage quota or private mode
  }
}

export function removeUserJson(baseKey: string): void {
  try {
    localStorage.removeItem(userScopedKey(baseKey))
  } catch {
    // ignore
  }
}

/** One-time migration from legacy sessionStorage key to per-user localStorage. */
export function migrateSessionToUser<T>(legacyKey: string, userBaseKey: string): T | null {
  const existing = readUserJson<T>(userBaseKey)
  if (existing !== null) return existing
  try {
    const raw = sessionStorage.getItem(legacyKey)
    if (!raw) return null
    const parsed = JSON.parse(raw) as T
    writeUserJson(userBaseKey, parsed)
    sessionStorage.removeItem(legacyKey)
    return parsed
  } catch {
    sessionStorage.removeItem(legacyKey)
    return null
  }
}
