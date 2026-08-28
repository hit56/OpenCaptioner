import { authHeaders } from './authHeaders'

export interface AdminUserUsage {
  userId: string
  displayName: string
  userName: string | null
  taskCount: number
  totalDurationSeconds: number
  lastActive: string | null
}

export interface OperationStats {
  totalDurationSeconds: number
  totalUsers: number
  totalTasks: number
  users: AdminUserUsage[]
}

interface ServerUserUsage {
  user_id?: string
  display_name?: string
  user_name?: string | null
  task_count?: number
  total_duration_seconds?: number
  last_active?: string | null
}

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

function mapServerUserUsage(row: ServerUserUsage): AdminUserUsage {
  const userId = String(row.user_id || '')
  return {
    userId,
    displayName: (row.display_name || '').trim() || userId,
    userName: row.user_name || null,
    taskCount: Number(row.task_count) || 0,
    totalDurationSeconds: Number(row.total_duration_seconds) || 0,
    lastActive: row.last_active || null,
  }
}

export async function fetchOperationStats(): Promise<OperationStats> {
  const response = await fetch('/api/admin/stats', {
    headers: authHeaders(),
  })
  const payload = await parseJsonSafe<{
    total_duration_seconds?: number
    total_users?: number
    total_tasks?: number
    users?: ServerUserUsage[]
    detail?: unknown
  }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '加载运营数据失败'))
  }
  const users = Array.isArray(payload.users) ? payload.users : []
  return {
    totalDurationSeconds: Number(payload.total_duration_seconds) || 0,
    totalUsers: Number(payload.total_users) || 0,
    totalTasks: Number(payload.total_tasks) || 0,
    users: users.filter((row) => row && row.user_id).map(mapServerUserUsage),
  }
}
