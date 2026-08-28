import type { UploadTaskResult } from '../../shared/types/asr'
import type { GlobalStatusState } from './globalStatusUtils'
import { isGatewayTaskId, isUploadTaskActive, resolveTaskHistoryStatus } from './taskMedia'

const BATCH_PROGRESS_RE = /第\s*(\d+)\/(\d+)\s*批次/

export function parseBatchProgressPercent(message: string): number | undefined {
  const match = message.match(BATCH_PROGRESS_RE)
  if (!match) return undefined
  const cur = parseInt(match[1], 10)
  const tot = parseInt(match[2], 10)
  if (!Number.isFinite(cur) || !Number.isFinite(tot) || tot <= 0) return undefined
  return Math.min(100, Math.max(0, ((cur - 1) / tot) * 100))
}

export function isTaskInFlight(task: UploadTaskResult): boolean {
  if (task.status === 'pending' || task.status === 'error') {
    return task.status === 'pending'
  }
  return isUploadTaskActive(task) || task.status === 'processing'
}

export function isTaskDashboardVisible(task: UploadTaskResult | undefined): boolean {
  if (!task) return false
  if (task.status === 'error' || resolveTaskHistoryStatus(task) === 'done') return false
  return task.status === 'pending' || task.status === 'processing' || isUploadTaskActive(task)
}

type ProgressLabels = {
  statusReady: string
  waitingTask: string
  prepareUpload: string
  uploading: string
  statusPending: string
  processing: string
  queuePosition: (position: number) => string
}

export function deriveTaskDashboard(
  task: UploadTaskResult,
  labels: ProgressLabels,
): GlobalStatusState {
  const title = task.fileName || labels.statusReady
  const phase = task.uploadPhase

  if (phase === 'waiting_upload') {
    return {
      visible: true,
      title,
      percentText: '0%',
      percentVal: 0,
      detailLeft: labels.statusPending,
    }
  }

  if (phase === 'uploading') {
    const percent = Math.round(task.uploadPercent ?? 0)
    return {
      visible: true,
      title,
      percentText: `${percent}%`,
      percentVal: percent,
      detailLeft: task.message || labels.uploading,
    }
  }

  if (phase === 'server_queued' && task.queuePosition && task.queuePosition > 0) {
    return {
      visible: true,
      title,
      percentText: labels.waitingTask,
      percentVal: 0,
      detailLeft: labels.queuePosition(task.queuePosition),
    }
  }

  const batchPercent = task.progressPercent ?? parseBatchProgressPercent(task.message)
  if (batchPercent != null) {
    const rounded = Math.round(batchPercent)
    return {
      visible: true,
      title,
      percentText: `${rounded}%`,
      percentVal: rounded,
      detailLeft: task.message || labels.processing,
    }
  }

  return {
    visible: true,
    title,
    percentText: task.uploadPhase === 'server_queued' ? labels.waitingTask : labels.processing,
    percentVal: task.progressPercent ?? 5,
    detailLeft: task.message || labels.processing,
  }
}

export function mergeTaskProgressEvent(
  task: UploadTaskResult,
  data: Record<string, unknown>,
): UploadTaskResult {
  const message = data.message ? String(data.message) : task.message
  const queuePosition =
    data.queue_position != null && Number.isFinite(Number(data.queue_position))
      ? Number(data.queue_position)
      : task.queuePosition
  const phaseRaw = data.phase ? String(data.phase) : ''
  let uploadPhase = task.uploadPhase
  if (phaseRaw === 'queued' || (queuePosition != null && queuePosition > 0)) {
    uploadPhase = 'server_queued'
  } else if (phaseRaw === 'processing' || queuePosition === 0) {
    uploadPhase = 'processing'
  }
  const progressPercent =
    (data.download_percent != null && Number.isFinite(Number(data.download_percent))
      ? Number(data.download_percent)
      : undefined) ??
    parseBatchProgressPercent(message) ??
    task.progressPercent
  return {
    ...task,
    message,
    queuePosition,
    uploadPhase,
    progressPercent,
  }
}

export function formatTaskSidebarHint(
  task: UploadTaskResult,
  labels: { uploading: (percent: number) => string; queuePosition: (position: number) => string },
): string | null {
  if (task.uploadPhase === 'waiting_upload') return null
  if (task.uploadPhase === 'uploading') {
    return labels.uploading(Math.round(task.uploadPercent ?? 0))
  }
  if (task.uploadPhase === 'server_queued' && task.queuePosition && task.queuePosition > 0) {
    return labels.queuePosition(task.queuePosition)
  }
  if (task.status === 'processing' && task.message && task.queuePosition && task.queuePosition > 0) {
    return labels.queuePosition(task.queuePosition)
  }
  return null
}

export function isClientSidePendingTask(task: UploadTaskResult): boolean {
  return task.status === 'pending' && !isGatewayTaskId(task.taskId)
}
