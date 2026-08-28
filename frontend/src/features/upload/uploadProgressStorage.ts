import { migrateSessionToUser, readUserJson, removeUserJson, writeUserJson } from '../../shared/storage/userStorage'
import type { GlobalStatusState } from './globalStatusUtils'
import { INITIAL_GLOBAL_STATUS } from './globalStatusUtils'

export interface PersistedUploadProgress {
  globalStatus: GlobalStatusState
  totalStartMs: number
  stageStartMs: number
  uploadStartMs: number | null
  fileNames: string
}

const UPLOAD_PROGRESS_KEY = 'react_upload_progress'
const LEGACY_UPLOAD_PROGRESS_KEY = 'react_upload_progress'

export function loadUploadProgress(): PersistedUploadProgress | null {
  const restored = migrateSessionToUser<PersistedUploadProgress>(
    LEGACY_UPLOAD_PROGRESS_KEY,
    UPLOAD_PROGRESS_KEY,
  )
  if (!restored) return null
  if (!Number.isFinite(restored.totalStartMs) || restored.totalStartMs <= 0) return null
  return {
    globalStatus: restored.globalStatus ?? INITIAL_GLOBAL_STATUS,
    totalStartMs: restored.totalStartMs,
    stageStartMs: Number.isFinite(restored.stageStartMs)
      ? restored.stageStartMs
      : restored.totalStartMs,
    uploadStartMs:
      restored.uploadStartMs != null && Number.isFinite(restored.uploadStartMs)
        ? restored.uploadStartMs
        : null,
    fileNames: restored.fileNames ?? '',
  }
}

export function writeUploadProgress(progress: PersistedUploadProgress): void {
  writeUserJson(UPLOAD_PROGRESS_KEY, progress)
}

export function clearUploadProgress(): void {
  removeUserJson(UPLOAD_PROGRESS_KEY)
}

export function readUploadProgressSnapshot(): PersistedUploadProgress | null {
  return readUserJson<PersistedUploadProgress>(UPLOAD_PROGRESS_KEY)
}
