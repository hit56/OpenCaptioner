export type LangCode = 'zh-CN' | 'en'

export interface UploadTaskQueuedResponse {
  status: string
  task_id: string
  file_url: string
  original_file_url?: string
  /** 网关创建任务时的 UTC ISO 时间 */
  created_at?: string
}

export interface UploadSegment {
  index: number
  timestamp: string
  text: string
  speaker?: string
  final: boolean
  segment_url?: string
  translation?: string
}

export interface TaskSpeakerStat {
  id: string
  duration: number
  gender?: string | null
}

export type UploadTaskPhase =
  | 'waiting_upload'
  | 'uploading'
  | 'server_queued'
  | 'processing'

export interface UploadTaskResult {
  taskId: string
  fileName: string
  fileUrl: string
  /** 服务端 uploads 原始文件 URL（含 user 段的新命名） */
  originalFileUrl?: string
  status: 'pending' | 'processing' | 'done' | 'error'
  message: string
  fullText: string
  segments: UploadSegment[]
  /** ISO 时间，用于历史列表排序展示 */
  createdAt?: string
  /** 源音频/视频时长（秒），done 事件写入 */
  mediaDurationSeconds?: number
  /** 烧录字幕后的视频（SSE done 事件 video_url） */
  videoUrl?: string
  /** 字幕重新刻印版本号（时间戳），用于刷新视频播放器破缓存 */
  subtitleVersion?: number
  /** 该任务内的搜索与说话人筛选 */
  keyword?: string
  speakerFilter?: string | null
  speakerStats?: TaskSpeakerStat[]
  detectedLang?: string
  detectedLangName?: string
  /** True when the user locked recognition language (skip auto-detect). */
  langForced?: boolean
  /** 客户端上传阶段进度 0–100 */
  uploadPercent?: number
  /** 服务端离线队列位次（1 起），0 表示正在处理 */
  queuePosition?: number
  /** ASR 批次进度 0–100 */
  progressPercent?: number
  uploadPhase?: UploadTaskPhase
}
