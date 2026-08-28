import type { UploadTaskResult, TaskSpeakerStat } from '../shared/types/asr'
import { appendClientUserQuery } from '../shared/storage/clientUser'
import { canonicalSubtitledVideoPath, isGatewayTaskId, normalizeUploadTaskCreatedAt } from '../features/upload/taskMedia'
import { authHeaders } from './authHeaders'
import { readUserJson, removeUserJson } from '../shared/storage/userStorage'

const UPLOAD_STATE_KEY = 'react_upload_state'

export interface ServerUploadTask {
  task_id: string
  file_name: string
  status: UploadTaskResult['status']
  message?: string
  created_at?: string | null
  file_url?: string | null
  original_file_url?: string | null
  video_url?: string | null
  media_duration_seconds?: number | null
  detected_lang?: string | null
  detected_lang_name?: string | null
  speaker_stats?: TaskSpeakerStat[] | null
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

export function mapServerUploadTask(row: ServerUploadTask): UploadTaskResult {
  const taskId = String(row.task_id || '')
  const fileName = String(row.file_name || taskId)
  const status = (row.status || 'done') as UploadTaskResult['status']
  const videoUrl = row.video_url
    ? canonicalSubtitledVideoPath(taskId)
    : undefined
  return normalizeUploadTaskCreatedAt({
    taskId,
    fileName,
    fileUrl: row.file_url ? appendClientUserQuery(String(row.file_url)) : '',
    originalFileUrl: row.original_file_url
      ? appendClientUserQuery(String(row.original_file_url))
      : undefined,
    status,
    message: String(row.message || ''),
    fullText: '',
    segments: [],
    createdAt: row.created_at || undefined,
    mediaDurationSeconds:
      row.media_duration_seconds != null && Number.isFinite(Number(row.media_duration_seconds))
        ? Number(row.media_duration_seconds)
        : undefined,
    videoUrl,
    detectedLang: row.detected_lang || undefined,
    detectedLangName: row.detected_lang_name || undefined,
    speakerStats: Array.isArray(row.speaker_stats) ? row.speaker_stats : undefined,
    uploadPhase: status === 'processing' ? 'processing' : undefined,
  })
}

export async function fetchMyUploadTasks(): Promise<UploadTaskResult[]> {
  const response = await fetch('/api/me/upload-tasks', {
    headers: authHeaders(),
  })
  const payload = await parseJsonSafe<{ tasks?: ServerUploadTask[]; detail?: unknown }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '加载历史任务失败'))
  }
  const tasks = Array.isArray(payload.tasks) ? payload.tasks : []
  return tasks
    .filter((item) => item?.task_id && isGatewayTaskId(String(item.task_id)))
    .map(mapServerUploadTask)
}

export async function deleteMyUploadTask(taskId: string): Promise<void> {
  const response = await fetch(`/api/me/upload-tasks/${encodeURIComponent(taskId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (response.ok || response.status === 404) return
  const payload = await parseJsonSafe<{ detail?: unknown }>(response)
  throw new Error(parseErrorDetail(payload, '删除历史任务失败'))
}

export async function migrateLegacyUploadTasks(tasks: UploadTaskResult[]): Promise<{
  imported: number
  skipped: number
}> {
  const gatewayTasks = tasks.filter((task) => isGatewayTaskId(task.taskId))
  if (!gatewayTasks.length) return { imported: 0, skipped: 0 }

  const response = await fetch('/api/me/upload-tasks/migrate', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      tasks: gatewayTasks.map((task) => ({
        task_id: task.taskId,
        file_name: task.fileName,
        status: task.status,
        message: task.message,
        created_at: task.createdAt,
        file_url: task.fileUrl?.split('?')[0] || null,
        original_file_url: task.originalFileUrl?.split('?')[0] || null,
        video_url: task.videoUrl ? canonicalSubtitledVideoPath(task.taskId) : null,
        media_duration_seconds: task.mediaDurationSeconds ?? null,
        detected_lang: task.detectedLang ?? null,
        detected_lang_name: task.detectedLangName ?? null,
        speaker_stats: task.speakerStats ?? null,
      })),
    }),
  })
  const payload = await parseJsonSafe<{
    imported?: number
    skipped?: number
    detail?: unknown
  }>(response)
  if (!response.ok) {
    throw new Error(parseErrorDetail(payload, '迁移历史任务失败'))
  }
  return {
    imported: Number(payload.imported || 0),
    skipped: Number(payload.skipped || 0),
  }
}

/** Read legacy localStorage history once, migrate to server, then clear. */
export async function migrateAndClearLegacyUploadTasks(): Promise<void> {
  const legacy = readUserJson<UploadTaskResult[]>(UPLOAD_STATE_KEY)
  if (!legacy?.length) {
    removeUserJson(UPLOAD_STATE_KEY)
    return
  }
  try {
    await migrateLegacyUploadTasks(legacy)
  } catch {
    // Keep local copy if migrate fails; next load can retry.
    return
  }
  removeUserJson(UPLOAD_STATE_KEY)
}
