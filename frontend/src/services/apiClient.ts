import type { UploadTaskQueuedResponse } from '../shared/types/asr'
import { getOrCreateClientUserId } from '../shared/storage/clientUser'
import { authHeaders, getAccessToken } from './authHeaders'

async function parseJsonSafe<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T
  } catch {
    return {} as T
  }
}

function parseUploadErrorDetail(responseText: string, fallback: string): string {
  try {
    const parsed = JSON.parse(responseText) as { detail?: unknown }
    const detail = parsed.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0] as { msg?: string }
      if (typeof first?.msg === 'string' && first.msg.trim()) return first.msg
    }
  } catch {
    // ignore malformed JSON
  }
  return fallback
}

export async function uploadFile(file: File, language: string): Promise<UploadTaskQueuedResponse> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('client_start_ms', String(Date.now()))
  formData.append('ui_language', language)
  formData.append('client_user_id', getOrCreateClientUserId())
  const response = await fetch('/upload', {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
  })
  const responseText = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(responseText, 'upload failed'))
  }
  try {
    return JSON.parse(responseText) as UploadTaskQueuedResponse
  } catch {
    return {} as UploadTaskQueuedResponse
  }
}

export async function uploadBilibiliUrl(
  url: string,
  language: string,
): Promise<UploadTaskQueuedResponse & { file_name?: string }> {
  const response = await fetch('/upload_bilibili', {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url,
      ui_language: language,
      client_user_id: getOrCreateClientUserId(),
      client_start_ms: String(Date.now()),
    }),
  })
  const responseText = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(responseText, 'upload failed'))
  }
  try {
    return JSON.parse(responseText) as UploadTaskQueuedResponse & { file_name?: string }
  } catch {
    return {} as UploadTaskQueuedResponse
  }
}

export async function uploadVideoUrl(
  url: string,
  language: string,
): Promise<UploadTaskQueuedResponse & { file_name?: string }> {
  const response = await fetch('/upload_video', {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url,
      ui_language: language,
      client_user_id: getOrCreateClientUserId(),
      client_start_ms: String(Date.now()),
    }),
  })
  const responseText = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(responseText, 'upload failed'))
  }
  try {
    return JSON.parse(responseText) as UploadTaskQueuedResponse & { file_name?: string }
  } catch {
    return {} as UploadTaskQueuedResponse
  }
}

export interface UploadProgressInfo {
  percent: number
  speedMbps: number
  startMs: number
}

export function uploadFileWithProgress(
  file: File,
  language: string,
  onProgress: (info: UploadProgressInfo) => void,
): Promise<UploadTaskQueuedResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const formData = new FormData()
    formData.append('file', file)
    const startMs = Date.now()
    formData.append('client_start_ms', String(startMs))
    formData.append('ui_language', language)
    formData.append('client_user_id', getOrCreateClientUserId())
    xhr.open('POST', '/upload', true)
    const token = getAccessToken()
    if (token) {
      xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    }
    xhr.upload.onprogress = (evt) => {
      if (!evt.lengthComputable) return
      const percent = (evt.loaded / evt.total) * 100
      const duration = (Date.now() - startMs) / 1000
      const speedMbps = duration > 0 ? evt.loaded / 1024 / 1024 / duration : 0
      onProgress({ percent, speedMbps, startMs })
    }
    xhr.onload = () => {
      if (xhr.status !== 200) {
        reject(new Error(parseUploadErrorDetail(xhr.responseText, 'upload failed')))
        return
      }
      try {
        const result = JSON.parse(xhr.responseText) as UploadTaskQueuedResponse
        if (result.status === 'queued') resolve(result)
        else reject(new Error('upload failed'))
      } catch {
        reject(new Error('upload failed'))
      }
    }
    xhr.onerror = () => reject(new Error('upload failed'))
    xhr.send(formData)
  })
}

export async function fetchTaskStatus(taskId: string): Promise<{
  exists?: boolean
  is_terminal?: boolean
} | null> {
  try {
    const userId = encodeURIComponent(getOrCreateClientUserId())
    const response = await fetch(
      `/task_status/${encodeURIComponent(taskId)}?client_user_id=${userId}`,
    )
    if (!response.ok) return null
    return await parseJsonSafe(response)
  } catch {
    return null
  }
}

export async function fetchTaskMediaInfo(taskId: string): Promise<{
  subtitled_available?: boolean
  video_url?: string | null
} | null> {
  try {
    const userId = encodeURIComponent(getOrCreateClientUserId())
    const response = await fetch(
      `/task_media_info/${encodeURIComponent(taskId)}?client_user_id=${userId}`,
    )
    if (!response.ok) return null
    return await parseJsonSafe(response)
  } catch {
    return null
  }
}

export async function fetchTaskSegmentResults(taskId: string, uiLanguage: string) {
  try {
    const params = new URLSearchParams({
      client_user_id: getOrCreateClientUserId(),
      ui_language: uiLanguage,
    })
    const response = await fetch(
      `/task_segment_results/${encodeURIComponent(taskId)}?${params.toString()}`,
    )
    if (!response.ok) return null
    return await parseJsonSafe<{
      segments: Array<{
        index: number
        timestamp?: string | null
        text?: string | null
        speaker?: string | null
        translation?: string | null
      }>
    }>(response)
  } catch {
    return null
  }
}

export interface SaveAndReburnResult {
  ok?: boolean
  video_url?: string | null
  changed_segments?: number
  cue_count?: number
  detail?: string
  reburning?: boolean
  job_id?: string
}

interface SubtitleReburnStatus {
  status?: 'idle' | 'queued' | 'running' | 'done' | 'error' | string
  job_id?: string | null
  error?: string | null
  video_url?: string | null
}

export interface SubtitleCuePayload {
  start: number
  end: number
  text: string
  trans?: string
}

export interface FetchSubtitleCuesResult {
  cues: SubtitleCuePayload[]
  duration: number
}

/** 加载可编辑的字幕条（cue）列表：来自强制对齐模型的真实音画对齐时间轴。 */
export async function fetchSubtitleCues(
  taskId: string,
): Promise<FetchSubtitleCuesResult | null> {
  try {
    const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
    const response = await fetch(
      `/task/${encodeURIComponent(taskId)}/subtitles/cues?${params.toString()}`,
      { headers: authHeaders() },
    )
    if (!response.ok) return null
    const data = await parseJsonSafe<{
      cues?: Array<{ start?: number; end?: number; text?: string; trans?: string }>
      duration?: number
    }>(response)
    const cues = (data.cues ?? []).map((c) => ({
      start: Number(c.start ?? 0),
      end: Number(c.end ?? 0),
      text: String(c.text ?? ''),
      trans: c.trans ? String(c.trans) : '',
    }))
    return { cues, duration: Number(data.duration ?? 0) }
  } catch {
    return null
  }
}

/** 时间轴编辑器草稿的 WebVTT 预览（按 cue 列表渲染，不落盘、不刻印）。失败返回 null。 */
export async function fetchSubtitleCuesDraftVtt(
  taskId: string,
  cues: SubtitleCuePayload[],
  signal?: AbortSignal,
): Promise<string | null> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  try {
    const response = await fetch(
      `/task/${encodeURIComponent(taskId)}/subtitles/cues/preview.vtt?${params.toString()}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ cues }),
        signal,
      },
    )
    if (!response.ok) return null
    return await response.text()
  } catch {
    return null
  }
}

/** 保存时间轴编辑器的 cue 列表；刻印在后台进行，这里轮询直到完成。 */
export async function saveSubtitleCues(
  taskId: string,
  cues: SubtitleCuePayload[],
  uiLanguage: string,
): Promise<SaveAndReburnResult> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const headers = authHeaders()
  headers.set('Content-Type', 'application/json')
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/subtitles/cues/save?${params.toString()}`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({ cues, ui_language: uiLanguage }),
    },
  )
  const text = await response.text()
  if (!response.ok) {
    if (response.status === 502 || response.status === 504) {
      throw new Error('网关超时，请稍后刷新查看字幕视频是否已更新')
    }
    throw new Error(parseUploadErrorDetail(text, '字幕保存失败'))
  }
  let accepted: SaveAndReburnResult = {}
  try {
    accepted = JSON.parse(text) as SaveAndReburnResult
  } catch {
    accepted = {}
  }
  if (!accepted.reburning || !accepted.job_id) {
    return accepted
  }
  return waitForSubtitleReburn(taskId, accepted)
}

const REBURN_POLL_MS = 1500
const REBURN_TIMEOUT_MS = 20 * 60 * 1000

async function waitForSubtitleReburn(
  taskId: string,
  accepted: SaveAndReburnResult,
): Promise<SaveAndReburnResult> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const started = Date.now()
  while (Date.now() - started < REBURN_TIMEOUT_MS) {
    await new Promise((resolve) => window.setTimeout(resolve, REBURN_POLL_MS))
    let status: SubtitleReburnStatus
    try {
      const response = await fetch(
        `/task/${encodeURIComponent(taskId)}/subtitles/reburn-status?${params.toString()}`,
        { headers: authHeaders() },
      )
      if (!response.ok) {
        throw new Error(parseUploadErrorDetail(await response.text(), '查询刻印状态失败'))
      }
      status = (await parseJsonSafe<SubtitleReburnStatus>(response)) || {}
    } catch (err) {
      if (Date.now() - started > 15_000) throw err
      continue
    }
    const phase = status.status || 'idle'
    if (phase === 'done') {
      return {
        ...accepted,
        ok: true,
        reburning: false,
        video_url: status.video_url || accepted.video_url,
      }
    }
    if (phase === 'error') {
      throw new Error(status.error || '字幕视频重新刻印失败')
    }
    if (phase === 'idle' && Date.now() - started > 15_000) {
      throw new Error('刻印状态丢失，请刷新后重试')
    }
  }
  throw new Error('重新刻印超时，请稍后刷新查看字幕视频是否已更新')
}

export interface BilibiliPublishStartResult {
  status?: string
  task_id?: string
  title?: string
  message?: string
  detail?: string
}

export interface BilibiliPublishStatus {
  task_id?: string
  status: 'idle' | 'publishing' | 'done' | 'error' | string
  message?: string
  progress?: number
  title?: string
  bvid?: string | null
  aid?: number | null
  url?: string | null
  error?: string | null
}

export type PublishCoverKind = 'generated' | 'frame_start' | 'frame_middle' | 'frame_end'

const PUBLISH_COVER_KINDS: PublishCoverKind[] = [
  'generated',
  'frame_start',
  'frame_middle',
  'frame_end',
]

function parseCoverKind(value: unknown): PublishCoverKind | null {
  if (value === 'frame') return 'frame_middle'
  if (typeof value === 'string' && (PUBLISH_COVER_KINDS as string[]).includes(value)) {
    return value as PublishCoverKind
  }
  return null
}

export interface BilibiliPublishMeta {
  task_id?: string
  title: string
  desc: string
  tags: string
  cached?: boolean
  cover_url?: string | null
  cover_available?: boolean
  cover_generated_available?: boolean
  cover_frame_available?: boolean
  cover_frames_available?: Partial<Record<'frame_start' | 'frame_middle' | 'frame_end', boolean>>
  cover_selected?: PublishCoverKind | null
  cover_generated_url?: string | null
  cover_frame_urls?: Partial<Record<'frame_start' | 'frame_middle' | 'frame_end', string>>
}

/** 获取投稿标题 / 简介 / 标签；默认复用缓存，refresh=true 强制重新生成。 */
export async function fetchBilibiliPublishMeta(
  taskId: string,
  options: { refresh?: boolean } = {},
): Promise<BilibiliPublishMeta> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  if (options.refresh) params.set('refresh', '1')
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/publish_bilibili/meta?${params.toString()}`,
    { headers: authHeaders() },
  )
  const text = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(text, '生成投稿文案失败'))
  }
  try {
    const data = JSON.parse(text) as Partial<BilibiliPublishMeta>
    return {
      task_id: data.task_id,
      title: String(data.title || '').trim(),
      desc: String(data.desc || '').trim(),
      tags: String(data.tags || '').trim(),
      cached: Boolean(data.cached),
      cover_url: data.cover_url || null,
      cover_available: Boolean(data.cover_available),
      cover_generated_available: Boolean(data.cover_generated_available),
      cover_frame_available: Boolean(data.cover_frame_available),
      cover_frames_available: data.cover_frames_available || {},
      cover_selected: parseCoverKind(data.cover_selected),
      cover_generated_url: data.cover_generated_url || null,
      cover_frame_urls: data.cover_frame_urls || {},
    }
  } catch {
    throw new Error('生成投稿文案失败')
  }
}

export interface BilibiliPublishCoverResult {
  task_id?: string
  cover_url?: string
  cover_kind?: PublishCoverKind | 'frame'
  cached?: boolean
  cover_generated_available?: boolean
  cover_frame_available?: boolean
  cover_frames_available?: Partial<Record<'frame_start' | 'frame_middle' | 'frame_end', boolean>>
  cover_selected?: PublishCoverKind | null
}

/** 生成投稿封面：source=generated 文生图；source=frame 从原视频随机抽三帧。 */
export async function generateBilibiliPublishCover(
  taskId: string,
  options: {
    title?: string
    desc?: string
    tags?: string
    refresh?: boolean
    source?: 'generated' | 'frame'
  } = {},
): Promise<BilibiliPublishCoverResult> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const source = options.source === 'frame' ? 'frame' : 'generated'
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/publish_bilibili/cover?${params.toString()}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        title: options.title,
        desc: options.desc,
        tags: options.tags,
        refresh: Boolean(options.refresh),
        source,
        client_user_id: getOrCreateClientUserId(),
      }),
    },
  )
  const text = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(text, source === 'frame' ? '抽帧封面失败' : '封面生成失败'))
  }
  try {
    return JSON.parse(text) as BilibiliPublishCoverResult
  } catch {
    return { cover_url: `/media/${taskId}/publish_cover?kind=${source}`, cover_kind: source, cached: false }
  }
}

/** 保存用户编辑后的投稿文案，供下次直接复用。 */
export async function saveBilibiliPublishMeta(
  taskId: string,
  meta: { title: string; desc?: string; tags?: string; cover_kind?: PublishCoverKind },
): Promise<BilibiliPublishMeta> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/publish_bilibili/meta?${params.toString()}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        title: meta.title,
        desc: meta.desc,
        tags: meta.tags,
        cover_kind: meta.cover_kind,
        client_user_id: getOrCreateClientUserId(),
      }),
    },
  )
  const text = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(text, '保存投稿文案失败'))
  }
  try {
    const data = JSON.parse(text) as Partial<BilibiliPublishMeta>
    return {
      task_id: data.task_id,
      title: String(data.title || meta.title || '').trim(),
      desc: String(data.desc ?? meta.desc ?? '').trim(),
      tags: String(data.tags ?? meta.tags ?? '').trim(),
      cached: true,
    }
  } catch {
    return {
      title: meta.title,
      desc: meta.desc || '',
      tags: meta.tags || '',
      cached: true,
    }
  }
}

/** 将字幕成片一键投稿到 B 站（异步，随后轮询 status）。 */
export async function publishTaskToBilibili(
  taskId: string,
  options: {
    title?: string
    desc?: string
    tags?: string
    tid?: number
    copyright?: number
    source?: string
  } = {},
): Promise<BilibiliPublishStartResult> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/publish_bilibili?${params.toString()}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        title: options.title,
        desc: options.desc,
        tags: options.tags,
        tid: options.tid,
        copyright: options.copyright,
        source: options.source,
        client_user_id: getOrCreateClientUserId(),
      }),
    },
  )
  const text = await response.text()
  if (!response.ok) {
    throw new Error(parseUploadErrorDetail(text, '投稿失败'))
  }
  try {
    return JSON.parse(text) as BilibiliPublishStartResult
  } catch {
    return { status: 'publishing', task_id: taskId }
  }
}

/** 查询 B 站投稿进度。 */
export async function fetchBilibiliPublishStatus(
  taskId: string,
): Promise<BilibiliPublishStatus> {
  const params = new URLSearchParams({ client_user_id: getOrCreateClientUserId() })
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/publish_bilibili/status?${params.toString()}`,
    { headers: authHeaders() },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new Error(parseUploadErrorDetail(text, '查询投稿状态失败'))
  }
  const data = await parseJsonSafe<BilibiliPublishStatus>(response)
  return {
    ...data,
    status: data.status || 'idle',
  }
}

