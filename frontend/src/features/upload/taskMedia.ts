import { appendClientUserQuery, clientUserIdTag, getOrCreateClientUserId } from '../../shared/storage/clientUser'
import type { LangCode, UploadTaskResult } from '../../shared/types/asr'

export type UploadTaskStatus = UploadTaskResult['status']
import { fetchTaskMediaInfo } from '../../services/apiClient'
import { parseSegmentRange } from './segmentTime'

const VIDEO_EXT = /\.(mp4|mov|avi|mkv|flv|webm|wmv|3gp|m4v)$/i

export function isVideoFileName(fileName: string): boolean {
  return VIDEO_EXT.test(fileName)
}

/** 持久化用：不带 client_user_id 的字幕成片路径 */
export function canonicalSubtitledVideoPath(taskId: string): string {
  return `/media/${encodeURIComponent(taskId)}/subtitled`
}

/** 字幕成片播放：走 FileResponse 端点，支持 Range 断点续传 */
export function buildSubtitledMediaUrl(taskId: string, version?: number): string {
  const base = appendClientUserQuery(canonicalSubtitledVideoPath(taskId))
  if (!version) return base
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}v=${version}`
}

/** 字幕成片下载：附带 download_subtitled=1，供网关记录高价值下载行为 */
export function buildSubtitledDownloadUrl(taskId: string): string {
  const base = buildSubtitledMediaUrl(taskId)
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}download_subtitled=1`
}

/** 投稿封面预览 / 下载 */
export function buildPublishCoverUrl(taskId: string, version?: number): string {
  const base = appendClientUserQuery(`/media/${encodeURIComponent(taskId)}/publish_cover`)
  if (!version) return base
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}v=${version}`
}

export function buildPublishCoverDownloadUrl(taskId: string): string {
  const base = buildPublishCoverUrl(taskId)
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}download=1`
}

/** 旁路导出字幕文件（SRT/ASS）：反映当前 final_result.json（含用户编辑），无需先刻印视频 */
export function buildSubtitledExportUrl(taskId: string, uiLanguage: string): string {
  const params = new URLSearchParams({
    client_user_id: getOrCreateClientUserId(),
    ui_language: uiLanguage,
  })
  return `/task/${encodeURIComponent(taskId)}/subtitles/export?${params.toString()}`
}

/** 播放器字幕预览轨（WebVTT）：与最终烧录同源，编辑后随 version 破缓存 */
export function buildSubtitlePreviewVttUrl(taskId: string, version?: number): string {
  const params = new URLSearchParams({
    client_user_id: getOrCreateClientUserId(),
  })
  if (version) params.set('v', String(version))
  return `/task/${encodeURIComponent(taskId)}/subtitles/preview.vtt?${params.toString()}`
}

/** 任务媒体（处理期间原片）：FileResponse + Range，避免 StaticFiles 断流 */
export function buildTaskMediaUrl(taskId: string): string {
  if (!isGatewayTaskId(taskId)) return ''
  return appendClientUserQuery(`/media/${encodeURIComponent(taskId)}`)
}

/** 从 task_id 解析 YYYYMMDDHHMMSS 与 uuid8，用于 uploads 文件名回退 */
function splitTaskIdParts(taskId: string): { ts: string; uuid8: string } | null {
  const compact = taskId.match(/^(\d{14})_([a-f0-9]{8})$/)
  if (compact) return { ts: compact[1], uuid8: compact[2] }
  const legacy = taskId.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_([a-f0-9]{8})$/)
  if (legacy) {
    return {
      ts: `${legacy[1]}${legacy[2]}${legacy[3]}${legacy[4]}${legacy[5]}${legacy[6]}`,
      uuid8: legacy[7],
    }
  }
  return null
}

/** 兼容旧数据：uploads 目录中的原始文件路径 */
export function buildOriginalFileUrl(
  taskId: string,
  fileName: string,
  originalFileUrl?: string,
): string {
  if (originalFileUrl) return appendClientUserQuery(originalFileUrl)
  const base = fileName.split('/').pop() || fileName
  const userTag = clientUserIdTag()
  const parts = splitTaskIdParts(taskId)
  if (parts && userTag) {
    const storedName = `${parts.ts}_${userTag}_${parts.uuid8}_${base}`
    return appendClientUserQuery(`/files/${encodeURI(storedName)}`)
  }
  const storedName = userTag ? `${taskId}_${userTag}_${base}` : `${taskId}_${base}`
  return appendClientUserQuery(`/files/${encodeURI(storedName)}`)
}

export function hasSubtitledVideo(task: {
  fileName: string
  videoUrl?: string
}): boolean {
  if (!isVideoFileName(task.fileName)) return false
  return Boolean(task.videoUrl)
}

/** 网关分配的真实 task_id（客户端排队占位 id 为 UUID） */
export function isGatewayTaskId(taskId: string): boolean {
  return Boolean(splitTaskIdParts(taskId))
}

/** 视频任务尚未收到字幕成片 URL */
export function isVideoAwaitingSubtitles(task: {
  fileName: string
  videoUrl?: string
}): boolean {
  return isVideoFileName(task.fileName) && !hasSubtitledVideo(task)
}

/** 历史列表展示用：压制字幕期间仍显示「处理中」 */
export function resolveTaskHistoryStatus(task: UploadTaskResult): UploadTaskStatus {
  if (task.status === 'pending') return 'pending'
  if (task.status === 'error') return 'error'
  if (task.status === 'done') return 'done'
  if (isVideoAwaitingSubtitles(task)) return 'processing'
  return task.status
}

const SUBTITLE_PROGRESS_RE =
  /正在生成字幕视频|字幕视频生成中|Generating subtitled video/i

/** 详情区状态文案：已完成任务不展示过期的压制进度提示 */
export function resolveTaskDisplayMessage(
  task: UploadTaskResult,
  labels: { done: string; processing: string },
): string {
  const status = resolveTaskHistoryStatus(task)
  if (status === 'done') {
    if (task.message.startsWith(labels.done) || !SUBTITLE_PROGRESS_RE.test(task.message)) {
      return task.message
    }
    return labels.done
  }
  if (status === 'error') return task.message
  return task.message || labels.processing
}

/** 任务是否仍在进行（含视频字幕压制阶段） */
export function isUploadTaskActive(task: UploadTaskResult): boolean {
  if (task.status === 'error' || task.status === 'pending' || task.status === 'done') {
    return false
  }
  return task.status === 'processing' || isVideoAwaitingSubtitles(task)
}

/** 视频播放：处理中用 /media/{taskId} 原片；字幕就绪后切到 /media/.../subtitled */
export function resolveVideoPlaybackUrl(
  task: {
    taskId: string
    fileName: string
    videoUrl?: string
    originalFileUrl?: string
    subtitleVersion?: number
  },
  options?: { preferOriginal?: boolean },
): string {
  if (!isVideoFileName(task.fileName) || !isGatewayTaskId(task.taskId)) return ''
  if (hasSubtitledVideo(task)) {
    if (!options?.preferOriginal) {
      return buildSubtitledMediaUrl(task.taskId, task.subtitleVersion)
    }
    return buildOriginalFileUrl(task.taskId, task.fileName, task.originalFileUrl)
  }
  return buildTaskMediaUrl(task.taskId)
}

export function resolveSubtitledDownloadUrl(task: {
  taskId: string
  fileName: string
  videoUrl?: string
}): string {
  if (!hasSubtitledVideo(task)) return ''
  return buildSubtitledDownloadUrl(task.taskId)
}

const TASK_ID_TIME_LEGACY_RE = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_/
const TASK_ID_TIME_COMPACT_RE = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_/

/** task_id 前缀与网关 session_id 一致，按 UTC 解析 */
export function parseTaskCreatedDateFromTaskId(taskId: string): Date | null {
  const compact = taskId.match(TASK_ID_TIME_COMPACT_RE)
  if (compact) {
    return new Date(
      Date.UTC(
        Number(compact[1]),
        Number(compact[2]) - 1,
        Number(compact[3]),
        Number(compact[4]),
        Number(compact[5]),
        Number(compact[6]),
      ),
    )
  }
  const legacy = taskId.match(TASK_ID_TIME_LEGACY_RE)
  if (!legacy) return null
  return new Date(
    Date.UTC(
      Number(legacy[1]),
      Number(legacy[2]) - 1,
      Number(legacy[3]),
      Number(legacy[4]),
      Number(legacy[5]),
      Number(legacy[6]),
    ),
  )
}

export function deriveCreatedAtFromTaskId(taskId: string): string | undefined {
  const date = parseTaskCreatedDateFromTaskId(taskId)
  return date ? date.toISOString() : undefined
}

/** 补全缺失或无效的 createdAt，避免回退解析时出现未来时间而直接显示绝对日期 */
export function normalizeUploadTaskCreatedAt<T extends { taskId: string; createdAt?: string }>(
  task: T,
): T {
  if (task.createdAt && Number.isFinite(Date.parse(task.createdAt))) return task
  const derived = deriveCreatedAtFromTaskId(task.taskId)
  return derived ? { ...task, createdAt: derived } : task
}

function parseTaskCreatedDate(taskId: string, createdAt?: string): Date | null {
  if (createdAt) {
    const parsed = Date.parse(createdAt)
    if (Number.isFinite(parsed)) return new Date(parsed)
  }
  return parseTaskCreatedDateFromTaskId(taskId)
}

function formatClockHm(date: Date): string {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function formatAbsoluteTaskTime(date: Date, lang: LangCode): string {
  if (lang === 'zh-CN') {
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${date.getHours()}时${date.getMinutes()}分${date.getSeconds()}秒`
  }
  return date.toLocaleString()
}

export function formatRelativeTaskTime(date: Date, now = new Date(), lang: LangCode = 'zh-CN'): string {
  const diffMs = now.getTime() - date.getTime()
  // 时钟偏差或 taskId 时区回退导致的轻微“未来时间”，仍按刚刚处理
  if (diffMs < 0) {
    if (diffMs > -5 * 60_000) return lang === 'zh-CN' ? '刚刚' : 'Just now'
    return formatAbsoluteTaskTime(date, lang)
  }

  const diffMin = diffMs / 60_000
  const diffHours = diffMs / 3_600_000

  if (diffMin < 5) return lang === 'zh-CN' ? '刚刚' : 'Just now'
  if (diffMin < 10) return lang === 'zh-CN' ? '5分钟前' : '5 min ago'
  if (diffMin < 15) return lang === 'zh-CN' ? '10分钟前' : '10 min ago'
  if (diffMin < 30) return lang === 'zh-CN' ? '15分钟前' : '15 min ago'
  if (diffMin < 60) return lang === 'zh-CN' ? '30分钟前' : '30 min ago'

  if (diffHours < 24) {
    const hours = Math.floor(diffMin / 60)
    if (lang === 'zh-CN') return `${hours}小时前`
    return hours === 1 ? '1 hour ago' : `${hours} hours ago`
  }

  const clock = formatClockHm(date)
  if (diffHours < 48) return lang === 'zh-CN' ? `昨天 ${clock}` : `Yesterday ${clock}`
  if (diffHours < 72) return lang === 'zh-CN' ? `前天 ${clock}` : `Day before yesterday ${clock}`

  return formatAbsoluteTaskTime(date, lang)
}

export function formatTaskListTime(
  taskId: string,
  createdAt?: string,
  options?: { now?: Date; lang?: LangCode },
): string {
  const date = parseTaskCreatedDate(taskId, createdAt)
  if (!date) return taskId
  return formatRelativeTaskTime(date, options?.now, options?.lang)
}

export function resolveTaskMediaDurationSeconds(task: UploadTaskResult): number | null {
  if (Number.isFinite(task.mediaDurationSeconds) && (task.mediaDurationSeconds ?? 0) > 0) {
    return task.mediaDurationSeconds as number
  }

  let maxEnd = 0
  for (const segment of task.segments) {
    const range = parseSegmentRange(segment.timestamp)
    if (range) maxEnd = Math.max(maxEnd, range.end)
  }
  if (maxEnd > 0) return maxEnd

  if (task.speakerStats?.length) {
    const total = task.speakerStats.reduce((sum, item) => sum + (item.duration || 0), 0)
    if (total > 0.05) return total
  }

  return null
}

/** 重新打开页面时，向服务端确认字幕成片是否仍在，并补全 videoUrl */
export async function hydrateUploadTaskMedia(task: UploadTaskResult): Promise<UploadTaskResult> {
  if (
    task.status === 'pending' ||
    task.status === 'error' ||
    !isVideoFileName(task.fileName) ||
    !isGatewayTaskId(task.taskId)
  ) {
    return task
  }

  if (task.videoUrl) {
    return { ...task, videoUrl: canonicalSubtitledVideoPath(task.taskId) }
  }

  const info = await fetchTaskMediaInfo(task.taskId)
  if (info?.subtitled_available) {
    return {
      ...task,
      videoUrl: info.video_url || canonicalSubtitledVideoPath(task.taskId),
    }
  }

  if (task.status === 'done') {
    return { ...task, status: 'processing' }
  }
  return task
}

export async function hydrateUploadTasksMedia(
  tasks: UploadTaskResult[],
): Promise<UploadTaskResult[]> {
  return Promise.all(tasks.map((task) => hydrateUploadTaskMedia(task)))
}
