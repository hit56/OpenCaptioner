import { authHeaders } from './authHeaders'
import { getOrCreateClientUserId } from '../shared/storage/clientUser'

export interface TaskSummaryResult {
  summary: string
  cached: boolean
}

function parseErrorDetail(payload: { detail?: unknown }, fallback: string): string {
  const detail = payload.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return fallback
}

/** 获取（或首次触发生成并缓存）视频转写摘要。 */
export async function fetchTaskSummary(
  taskId: string,
  uiLanguage: string,
): Promise<TaskSummaryResult> {
  const params = new URLSearchParams({
    client_user_id: getOrCreateClientUserId(),
    ui_language: uiLanguage,
  })
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/summary?${params.toString()}`,
    { headers: authHeaders() },
  )
  let payload: { summary?: string; cached?: boolean; detail?: unknown } = {}
  try {
    payload = (await response.json()) as typeof payload
  } catch {
    payload = {}
  }
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '摘要生成失败'))
  }
  return {
    summary: String(payload.summary ?? ''),
    cached: Boolean(payload.cached),
  }
}
