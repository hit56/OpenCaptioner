import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { createSseTaskStream } from '../../services/sse'
import { fetchTaskSegmentResults, fetchTaskStatus, uploadFileWithProgress, uploadVideoUrl } from '../../services/apiClient'
import {
  deleteMyUploadTask,
  fetchMyUploadTasks,
  migrateAndClearLegacyUploadTasks,
} from '../../services/uploadHistoryApi'
import { useI18n } from '../../shared/i18n/useI18n'
import type { UploadTaskResult } from '../../shared/types/asr'
import { UploadTaskDetailPanel } from './UploadTaskDetailPanel'
import { UploadTaskHistoryPanel } from './UploadTaskHistoryPanel'
import { segmentMatchesFilters } from './segmentVisibility'
import { mergeSegmentTranslations, parseSegmentTranslations } from './segmentTranslations'
import { isValidDetectedLang } from './speakerStatsUtils'
import { useSegmentPlayer } from './useSegmentPlayer'
import { GlobalStatusDashboard } from './GlobalStatusDashboard'
import {
  INITIAL_GLOBAL_STATUS,
  formatSpkDuration,
  parseDurationToSeconds,
} from './globalStatusUtils'
import { useGlobalStageTimer } from './useGlobalStageTimer'
import {
  clearUploadProgress,
  loadUploadProgress,
  writeUploadProgress,
  type PersistedUploadProgress,
} from './uploadProgressStorage'
import { appendClientUserQuery } from '../../shared/storage/clientUser'
import {
  isVideoFileName,
  hydrateUploadTasksMedia,
  canonicalSubtitledVideoPath,
  isUploadTaskActive,
  isGatewayTaskId,
  resolveTaskHistoryStatus,
} from './taskMedia'
import {
  deriveTaskDashboard,
  isClientSidePendingTask,
  isTaskDashboardVisible,
  isTaskInFlight,
  mergeTaskProgressEvent,
} from './taskProgress'
import { useAppState } from '../../app/AppState'
import { logUserClick } from '../../services/clickLog'
import {
  ASR_LANGUAGE_OPTIONS,
  asrLanguageLabelKey,
  loadAsrLanguage,
  saveAsrLanguage,
  type AsrLanguageCode,
} from './asrLanguage'

const MAX_UPLOAD_FILES = 6

interface QueueItem {
  id: string
  file: File
  asrLanguage: AsrLanguageCode
}

function hasActiveUploadWork(tasks: UploadTaskResult[], processing: boolean, queueLen: number): boolean {
  return (
    processing ||
    queueLen > 0 ||
    tasks.some((task) => task.status === 'pending' || isUploadTaskActive(task))
  )
}

function mergeTaskRecognitionPayload(
  task: UploadTaskResult,
  taskId: string,
  data: Record<string, unknown>,
): UploadTaskResult {
  const finalSegments = Array.isArray(data.final_segments) ? data.final_segments : []
  const segmentTranslations = parseSegmentTranslations(data.segment_translations)
  let segments = task.segments
  if (finalSegments.length) {
    segments = finalSegments
      .map((segment, i) => {
        const item = segment as Record<string, unknown>
        return {
          index: Number(item.index ?? i),
          timestamp: String(item.timestamp || ''),
          text: String(item.text || ''),
          speaker: item.speaker != null ? String(item.speaker) : undefined,
          final: true,
          segment_url: item.segment_url
            ? appendClientUserQuery(String(item.segment_url))
            : undefined,
          translation: item.translation ? String(item.translation) : undefined,
        }
      })
      .sort((a, b) => a.index - b.index)
  }
  segments = mergeSegmentTranslations(segments, segmentTranslations)
  const curAudio = parseDurationToSeconds(data.audio_duration)
  return {
    ...task,
    mediaDurationSeconds: curAudio > 0 ? curAudio : task.mediaDurationSeconds,
    videoUrl:
      Boolean(data.video_url) && isVideoFileName(task.fileName)
        ? canonicalSubtitledVideoPath(taskId)
        : task.videoUrl,
    segments,
    fullText: segments
      .filter((segment) => segment.final)
      .map((segment) => segment.text)
      .join(''),
  }
}

function loadRestorableUploadProgress(tasks: UploadTaskResult[]): PersistedUploadProgress | null {
  const saved = loadUploadProgress()
  if (!saved) return null
  if (!tasks.some((task) => isTaskInFlight(task))) {
    clearUploadProgress()
    return null
  }
  return saved
}

export function UploadTab() {
  const { t, lang } = useI18n()
  const { store, dispatch } = useAppState()
  const [historySlot, setHistorySlot] = useState<HTMLElement | null>(null)
  const savedProgressRef = useRef<PersistedUploadProgress | null>(null)
  const { playingKey, togglePlay } = useSegmentPlayer()
  const tasksCountRef = useRef(0)
  const batchStatsRef = useRef({ audio: 0, proc: 0, total: 0 })
  const uploadStartMsRef = useRef<number | null>(null)
  const hideDashboardTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const queueRef = useRef<QueueItem[]>([])
  const listeningTaskIdsRef = useRef(new Set<string>())
  const hydratedTranslationsRef = useRef(new Set<string>())
  const historyLoadedRef = useRef(false)
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [tasks, setTasks] = useState<UploadTaskResult[]>([])
  const tasksRef = useRef(tasks)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [processing, setProcessing] = useState(false)
  const [fileNames, setFileNames] = useState('')
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [biliUrl, setBiliUrl] = useState('')
  const [biliSubmitting, setBiliSubmitting] = useState(false)
  const [biliError, setBiliError] = useState<string | null>(null)
  const [uploadLimitNotice, setUploadLimitNotice] = useState<string | null>(null)
  const [asrLang, setAsrLang] = useState<AsrLanguageCode>(loadAsrLanguage)
  const asrLangRef = useRef(asrLang)
  asrLangRef.current = asrLang
  const [historyLoading, setHistoryLoading] = useState(true)
  const { timerText, startTimer, resumeTimer, stopTimer, resetTimerClocks, getTimerClocks } =
    useGlobalStageTimer(t('totalTime'))

  const progressLabels = useMemo(
    () => ({
      statusReady: t('statusReady'),
      waitingTask: t('waitingPercent'),
      prepareUpload: t('prepareUpload'),
      uploading: t('uploading'),
      statusPending: t('statusPending'),
      processing: t('processing'),
      queuePosition: (position: number) => t('queuePosition').replace('{0}', String(position)),
    }),
    [t],
  )

  const selectedTask = useMemo(
    () => tasks.find((task) => task.taskId === selectedTaskId),
    [tasks, selectedTaskId],
  )

  const taskDashboard = useMemo(() => {
    if (!selectedTask || !isTaskDashboardVisible(selectedTask)) {
      return { ...INITIAL_GLOBAL_STATUS, visible: false }
    }
    return deriveTaskDashboard(selectedTask, progressLabels)
  }, [progressLabels, selectedTask])

  const showBatchComplete = useCallback(() => {
    stopTimer(false)
    resetTimerClocks()
    clearUploadProgress()
    if (hideDashboardTimerRef.current) {
      clearTimeout(hideDashboardTimerRef.current)
      hideDashboardTimerRef.current = null
    }
    setFileNames('')
  }, [resetTimerClocks, stopTimer])

  const flushUploadProgress = useCallback(() => {
    const clocks = getTimerClocks()
    if (
      !hasActiveUploadWork(tasksRef.current, processing, queueRef.current.length) ||
      !clocks ||
      !taskDashboard.visible
    ) {
      clearUploadProgress()
      return
    }
    writeUploadProgress({
      globalStatus: taskDashboard,
      totalStartMs: clocks.totalStartMs,
      stageStartMs: clocks.stageStartMs,
      uploadStartMs: uploadStartMsRef.current,
      fileNames,
    })
  }, [fileNames, getTimerClocks, processing, taskDashboard])

  useEffect(() => {
    setHistorySlot(document.getElementById('sidebar-upload-history-slot'))
  }, [])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      setHistoryLoading(true)
      try {
        await migrateAndClearLegacyUploadTasks()
        if (cancelled) return
        const remote = await fetchMyUploadTasks()
        if (cancelled) return
        const hydrated = await hydrateUploadTasksMedia(remote)
        if (cancelled) return
        setTasks(hydrated)
        historyLoadedRef.current = true
        if (hydrated.length) {
          dispatch({ type: 'toggle-sidebar-open', open: true })
          dispatch({ type: 'toggle-upload-history', open: true })
          const newest = [...hydrated].sort((a, b) => {
            const ta = a.createdAt ? Date.parse(a.createdAt) : 0
            const tb = b.createdAt ? Date.parse(b.createdAt) : 0
            if (tb !== ta) return tb - ta
            return b.taskId.localeCompare(a.taskId)
          })[0]
          setSelectedTaskId(newest.taskId)
        }
        const restoredProgress = loadRestorableUploadProgress(hydrated)
        savedProgressRef.current = restoredProgress
        if (restoredProgress) {
          setFileNames(restoredProgress.fileNames ?? '')
          uploadStartMsRef.current = restoredProgress.uploadStartMs
          resumeTimer({
            totalStartMs: restoredProgress.totalStartMs,
            stageStartMs: restoredProgress.stageStartMs,
          })
        }
      } catch (error) {
        console.warn('Failed to load upload history', error)
      } finally {
        if (!cancelled) setHistoryLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [dispatch, resumeTimer])

  useEffect(() => {
    if (!historyLoadedRef.current) return
    void (async () => {
      const candidates = tasksRef.current.filter(
        (task) => isGatewayTaskId(task.taskId) && isUploadTaskActive(task),
      )
      if (!candidates.length) return

      const staleIds = new Set<string>()
      await Promise.all(
        candidates.map(async (task) => {
          const status = await fetchTaskStatus(task.taskId)
          if (status?.exists === false) staleIds.add(task.taskId)
        }),
      )
      if (!staleIds.size) return

      setTasks((prev) =>
        prev.map((task) =>
          staleIds.has(task.taskId)
            ? {
                ...task,
                status: 'error' as const,
                uploadPhase: undefined,
                message: `${t('failed')}: Task not found`,
              }
            : task,
        ),
      )
    })()
  }, [historyLoading, t])

  useEffect(() => {
    tasksRef.current = tasks
  }, [tasks])

  useEffect(() => {
    if (!hasActiveUploadWork(tasks, processing, queue.length)) {
      clearUploadProgress()
      if (!hasActiveUploadWork(tasks, processing, queue.length)) {
        setFileNames('')
      }
      return
    }
    flushUploadProgress()
    const intervalId = window.setInterval(flushUploadProgress, 2000)
    return () => window.clearInterval(intervalId)
  }, [flushUploadProgress, processing, queue.length, tasks])

  useEffect(
    () => () => {
      flushUploadProgress()
    },
    [flushUploadProgress],
  )

  useEffect(() => {
    tasksCountRef.current = tasks.length
  }, [tasks.length])

  useEffect(() => {
    queueRef.current = queue
  }, [queue])

  useEffect(
    () => () => {
      if (hideDashboardTimerRef.current) clearTimeout(hideDashboardTimerRef.current)
    },
    [],
  )

  useEffect(() => {
    if (!selectedTask || !isTaskDashboardVisible(selectedTask)) {
      stopTimer(false)
      return
    }
    if (selectedTask.uploadPhase === 'uploading') return
    startTimer()
  }, [selectedTask, startTimer, stopTimer])

  const listenTask = useCallback(
    (taskId: string, fileName: string, onComplete: () => void) => {
      const source = createSseTaskStream(taskId)
      const patchTaskProgress = (patch: Partial<UploadTaskResult>) => {
        setTasks((prev) =>
          prev.map((task) => (task.taskId === taskId ? { ...task, ...patch } : task)),
        )
      }
      source.onmessage = (event) => {
        const data = JSON.parse(event.data) as Record<string, unknown>
        if (data.type === 'speaker_stats') {
          const speakers = Array.isArray(data.speakers)
            ? (data.speakers as Array<Record<string, unknown>>).map((item) => ({
                id: String(item.id ?? '0'),
                duration: Number(item.duration || 0),
                gender: item.gender ? String(item.gender) : null,
              }))
            : []
          const langCode = data.detected_lang ? String(data.detected_lang) : undefined
          const serverForced = Boolean(data.lang_forced)
          const langName = isValidDetectedLang(langCode)
            ? data.detected_lang_name
              ? String(data.detected_lang_name)
              : langCode
            : undefined
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              const keepForced = Boolean(task.langForced) && !serverForced
              return {
                ...task,
                speakerStats: speakers,
                detectedLang: keepForced ? task.detectedLang : langName ? langCode : undefined,
                detectedLangName: keepForced ? task.detectedLangName : langName,
                langForced: task.langForced || serverForced,
              }
            }),
          )
        }
        if (data.type === 'segment_raw' || data.type === 'segment_final') {
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              const incomingTranslation = data.translation
                ? String(data.translation)
                : undefined
              const segment = {
                index: Number(data.index || 0),
                timestamp: String(data.timestamp || ''),
                text: String(data.text || ''),
                speaker: data.speaker ? String(data.speaker) : undefined,
                final: data.type === 'segment_final',
                segment_url: data.segment_url
                  ? appendClientUserQuery(String(data.segment_url))
                  : undefined,
                translation: incomingTranslation,
              }
              const idx = task.segments.findIndex((s) => s.index === segment.index)
              const segments = [...task.segments]
              if (idx >= 0) {
                const existing = segments[idx]
                segments[idx] = {
                  ...segment,
                  translation: incomingTranslation ?? existing.translation,
                }
              } else {
                segments.push(segment)
              }
              segments.sort((a, b) => a.index - b.index)
              return {
                ...task,
                segments,
                fullText: segments
                  .filter((s) => s.final)
                  .map((s) => s.text)
                  .join(''),
                message: String(data.message || task.message),
              }
            }),
          )
        }
        if (data.type === 'segment_translation') {
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              const index = Number(data.index ?? -1)
              const translation = String(data.translation || '')
              if (index < 0 || !translation) return task
              const segments = task.segments.map((segment) =>
                segment.index === index ? { ...segment, translation } : segment,
              )
              return { ...task, segments }
            }),
          )
        }
        if (data.type === 'progress') {
          const current = tasksRef.current.find((task) => task.taskId === taskId)
          const taskAlreadyDone =
            current != null && resolveTaskHistoryStatus(current) === 'done'
          if (taskAlreadyDone) return
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              return mergeTaskProgressEvent(
                { ...task, status: task.status === 'pending' ? 'processing' : task.status },
                data,
              )
            }),
          )
        }
        if (data.type === 'asr_done') {
          const message = data.message ? String(data.message) : t('subtitledVideoPending')
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              return {
                ...mergeTaskRecognitionPayload(task, taskId, data),
                ...mergeTaskProgressEvent(task, { ...data, message, phase: 'processing', queue_position: 0 }),
                status: 'processing',
                message,
              }
            }),
          )
        }
        if (data.type === 'done') {
          const waitingSubtitles = isVideoFileName(fileName) && !Boolean(data.video_url)
          if (waitingSubtitles) {
            const message = data.message ? String(data.message) : t('subtitledVideoPending')
            setTasks((prev) =>
              prev.map((task) => {
                if (task.taskId !== taskId) return task
                return {
                  ...mergeTaskRecognitionPayload(task, taskId, data),
                  status: 'processing',
                  uploadPhase: 'processing',
                  message,
                }
              }),
            )
            return
          }
          source.close()
          const curAudio = parseDurationToSeconds(data.audio_duration)
          const curProc = Number.isFinite(data.proc_duration_seconds)
            ? Number(data.proc_duration_seconds)
            : parseDurationToSeconds(data.proc_duration)
          const serverTotal = Number.isFinite(data.total_duration_seconds)
            ? Number(data.total_duration_seconds)
            : parseDurationToSeconds(data.total_duration)
          const uploadStartMs = uploadStartMsRef.current
          const clientTotal = uploadStartMs ? (Date.now() - uploadStartMs) / 1000 : 0
          const curTotal = Math.max(serverTotal, clientTotal, curProc)
          const prettyTotal = uploadStartMs
            ? formatSpkDuration(curTotal)
            : String(data.total_duration || formatSpkDuration(curTotal))
          batchStatsRef.current.audio += curAudio
          batchStatsRef.current.proc += curProc
          batchStatsRef.current.total += curTotal
          const statMsg = `${t('totalTime')}: ${prettyTotal}`
          setTasks((prev) =>
            prev.map((task) => {
              if (task.taskId !== taskId) return task
              return {
                ...mergeTaskRecognitionPayload(task, taskId, data),
                status: 'done' as const,
                uploadPhase: undefined,
                progressPercent: 100,
                queuePosition: 0,
                message: `${t('done')}(${statMsg})`,
              }
            }),
          )
          onComplete()
        }
        if (data.type === 'error') {
          source.close()
          patchTaskProgress({
            status: 'error',
            uploadPhase: undefined,
            message: `${t('failed')}: ${String(data.message || '')}`,
          })
          onComplete()
        }
      }
      source.onerror = () => {
        void (async () => {
          const status = await fetchTaskStatus(taskId)
          if (!status?.is_terminal) return
          patchTaskProgress({ message: t('taskDoneInBg') })
        })()
      }
    },
    [t],
  )

  useEffect(() => {
    for (const task of tasks) {
      if (
        !isUploadTaskActive(task) ||
        !isGatewayTaskId(task.taskId) ||
        listeningTaskIdsRef.current.has(task.taskId)
      ) {
        continue
      }
      listeningTaskIdsRef.current.add(task.taskId)
      listenTask(task.taskId, task.fileName, () => {
        listeningTaskIdsRef.current.delete(task.taskId)
      })
    }
  }, [listenTask, tasks])

  useEffect(() => {
    if (!selectedTaskId || hydratedTranslationsRef.current.has(selectedTaskId)) return
    const task = tasks.find((item) => item.taskId === selectedTaskId)
    if (!task || task.status !== 'done' || !isGatewayTaskId(selectedTaskId)) return
    const needsFullSegments = task.segments.length === 0
    const needsTranslations = task.segments.some((segment) => segment.text && !segment.translation)
    if (!needsFullSegments && !needsTranslations) {
      hydratedTranslationsRef.current.add(selectedTaskId)
      return
    }

    void (async () => {
      const result = await fetchTaskSegmentResults(selectedTaskId, lang)
      hydratedTranslationsRef.current.add(selectedTaskId)
      if (!result?.segments?.length) return
      const incoming = result.segments.map((segment, i) => ({
        index: Number(segment.index ?? i),
        timestamp: String(segment.timestamp || ''),
        text: String(segment.text || ''),
        speaker: segment.speaker != null ? String(segment.speaker) : undefined,
        final: true,
        translation: segment.translation ? String(segment.translation) : undefined,
      }))
      setTasks((prev) =>
        prev.map((item) => {
          if (item.taskId !== selectedTaskId) return item
          const segments = needsFullSegments
            ? incoming.sort((a, b) => a.index - b.index)
            : mergeSegmentTranslations(
                item.segments,
                incoming
                  .filter((segment) => segment.translation)
                  .map((segment) => ({
                    index: segment.index,
                    translation: String(segment.translation),
                  })),
              )
          return {
            ...item,
            segments,
            fullText: segments
              .filter((segment) => segment.final)
              .map((segment) => segment.text)
              .join(''),
          }
        }),
      )
    })()
  }, [lang, selectedTaskId, tasks])

  useEffect(() => {
    if (processing || !queue.length) return
    const current = queue[0]

    setProcessing(true)
    setQueue((prev) => prev.slice(1))
    if (hideDashboardTimerRef.current) {
      clearTimeout(hideDashboardTimerRef.current)
      hideDashboardTimerRef.current = null
    }

    setTasks((prev) =>
      prev.map((task) =>
        task.taskId === current.id
          ? {
              ...task,
              uploadPhase: 'uploading' as const,
              uploadPercent: 0,
              message: t('prepareUpload'),
            }
          : task,
      ),
    )

    void (async () => {
      try {
        const queued = await uploadFileWithProgress(
          current.file,
          lang,
          ({ percent, speedMbps, startMs }) => {
            uploadStartMsRef.current = startMs
            setTasks((prev) =>
              prev.map((task) =>
                task.taskId === current.id
                  ? {
                      ...task,
                      uploadPhase: 'uploading' as const,
                      uploadPercent: percent,
                      message: t('uploadSpeed').replace('{0}', speedMbps.toFixed(2)),
                    }
                  : task,
              ),
            )
          },
          current.asrLanguage,
        )
        if (queued.status !== 'queued') throw new Error(t('uploadError'))

        const newTask: UploadTaskResult = {
          taskId: queued.task_id,
          fileName: current.file.name,
          fileUrl: appendClientUserQuery(queued.file_url),
          originalFileUrl: queued.original_file_url
            ? appendClientUserQuery(queued.original_file_url)
            : undefined,
          status: 'processing',
          uploadPhase: 'server_queued',
          uploadPercent: 100,
          message: t('serverProcessing'),
          fullText: '',
          segments: [],
          createdAt: queued.created_at || new Date().toISOString(),
          ...(current.asrLanguage !== 'auto'
            ? {
                detectedLang: current.asrLanguage,
                detectedLangName: t(asrLanguageLabelKey(current.asrLanguage) || current.asrLanguage),
                langForced: true,
              }
            : {}),
        }
        setTasks((prev) => prev.map((task) => (task.taskId === current.id ? newTask : task)))
        setSelectedTaskId((prev) => (prev === current.id ? queued.task_id : prev))

        listeningTaskIdsRef.current.add(queued.task_id)
        listenTask(queued.task_id, current.file.name, () => {
          listeningTaskIdsRef.current.delete(queued.task_id)
        })

        setProcessing(false)
        if (
          queueRef.current.length === 0 &&
          !tasksRef.current.some((task) => isTaskInFlight(task))
        ) {
          showBatchComplete()
        }
      } catch (error) {
        setTasks((prev) =>
          prev.map((task) =>
            task.taskId === current.id
              ? {
                  ...task,
                  status: 'error' as const,
                  uploadPhase: undefined,
                  message: `${t('uploadError')}: ${(error as Error).message}`,
                }
              : task,
          ),
        )
        setProcessing(false)
        if (queueRef.current.length === 0) showBatchComplete()
      }
    })()
  }, [lang, listenTask, processing, queue, showBatchComplete, t])

  function patchTask(taskId: string, patch: Partial<UploadTaskResult>) {
    setTasks((prev) =>
      prev.map((task) => (task.taskId === taskId ? { ...task, ...patch } : task)),
    )
  }

  function enqueueFiles(files: File[]) {
    if (!files.length || uploadLocked) return
    let accepted = files
    if (files.length > MAX_UPLOAD_FILES) {
      accepted = files.slice(0, MAX_UPLOAD_FILES)
      setUploadLimitNotice(t('uploadMaxFiles').replaceAll('{0}', String(MAX_UPLOAD_FILES)))
    } else {
      setUploadLimitNotice(null)
    }
    dispatch({ type: 'toggle-sidebar-open', open: true })
    dispatch({ type: 'toggle-upload-history', open: true })
    const langAtSelect = asrLangRef.current
    const newItems = accepted.map((file) => ({
      id: crypto.randomUUID(),
      file,
      asrLanguage: langAtSelect,
    }))
    setFileNames(accepted.map((f) => f.name).join(', '))
    batchStatsRef.current = { audio: 0, proc: 0, total: 0 }
    clearUploadProgress()
    resetTimerClocks()
    setQueue((prev) => [...prev, ...newItems])
    const batchBaseMs = Date.now()
    setTasks((prev) => [
      ...newItems.map((item, index) => ({
        taskId: item.id,
        fileName: item.file.name,
        fileUrl: '',
        status: 'pending' as const,
        uploadPhase: 'waiting_upload' as const,
        message: t('statusPending'),
        fullText: '',
        segments: [],
        createdAt: new Date(batchBaseMs - (newItems.length - 1 - index) * 1000).toISOString(),
      })),
      ...prev,
    ])
    setSelectedTaskId(newItems[0].id)
  }

  const uploadLocked = useMemo(
    () =>
      processing ||
      queue.length > 0 ||
      tasks.some((task) => task.status === 'pending' || isUploadTaskActive(task)),
    [processing, queue.length, tasks],
  )

  const showBackgroundNotice = useMemo(
    () =>
      tasks.some(
        (task) =>
          isGatewayTaskId(task.taskId) &&
          task.status !== 'error' &&
          (task.status === 'processing' || isUploadTaskActive(task)),
      ),
    [tasks],
  )

  const showTaskTools =
    Boolean(selectedTask) &&
    queue.length === 0 &&
    (selectedTask!.segments.length > 0 ||
      resolveTaskHistoryStatus(selectedTask!) === 'done' ||
      selectedTask!.status === 'error')

  // 字幕可编辑：已完成的网关视频任务（已有字幕成片）
  const subtitleEditable =
    Boolean(selectedTask) &&
    isVideoFileName(selectedTask!.fileName) &&
    isGatewayTaskId(selectedTask!.taskId) &&
    resolveTaskHistoryStatus(selectedTask!) === 'done' &&
    Boolean(selectedTask!.videoUrl)

  function toggleSpeakerFilter(speakerId: string) {
    if (!selectedTaskId) return
    const current = selectedTask?.speakerFilter ?? null
    patchTask(selectedTaskId, { speakerFilter: current === speakerId ? null : speakerId })
  }

  function deleteSpeaker(speakerId: string) {
    if (!selectedTaskId || !selectedTask) return
    const segments = selectedTask.segments.filter((segment) => {
      const spk =
        segment.speaker !== undefined && segment.speaker !== null
          ? String(segment.speaker)
          : 'unknown'
      return spk !== speakerId
    })
    patchTask(selectedTaskId, {
      segments,
      fullText: segments
        .filter((s) => s.final)
        .map((s) => s.text)
        .join(''),
      speakerFilter: selectedTask.speakerFilter === speakerId ? null : selectedTask.speakerFilter,
    })
  }

  function setTaskKeyword(value: string) {
    if (!selectedTaskId) return
    patchTask(selectedTaskId, { keyword: value })
  }

  function deleteTask(taskId: string) {
    const target = tasks.find((task) => task.taskId === taskId)
    if (target && isClientSidePendingTask(target)) {
      setQueue((prev) => prev.filter((item) => item.id !== taskId))
    }
    if (target && isGatewayTaskId(taskId)) {
      void deleteMyUploadTask(taskId).catch((error) => {
        console.warn('Failed to delete upload task on server', error)
      })
    }
    setTasks((prev) => {
      const next = prev.filter((task) => task.taskId !== taskId)
      if (selectedTaskId === taskId) {
        const newest = [...next].sort((a, b) => {
          const ta = a.createdAt ? Date.parse(a.createdAt) : 0
          const tb = b.createdAt ? Date.parse(b.createdAt) : 0
          if (tb !== ta) return tb - ta
          return b.taskId.localeCompare(a.taskId)
        })[0]
        setSelectedTaskId(newest?.taskId ?? null)
      }
      return next
    })
  }

  function copyVisibleResults(withTime: boolean): boolean {
    if (!selectedTask) return false
    const kw = selectedTask.keyword ?? ''
    const spkFilter = selectedTask.speakerFilter ?? null
    const lines: string[] = []
    for (const segment of selectedTask.segments) {
      if (!segmentMatchesFilters(segment, kw, spkFilter)) continue
      if (withTime && segment.timestamp) {
        lines.push(`${segment.timestamp} ${segment.text}`)
      } else {
        lines.push(segment.text)
      }
    }
    if (!lines.length && selectedTask.fullText?.trim()) {
      lines.push(selectedTask.fullText.trim())
    }
    if (!lines.length) return false
    const text = lines.join('\n').trim()
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).catch(() => undefined)
      return true
    }
    const textArea = document.createElement('textarea')
    textArea.value = text
    textArea.style.position = 'fixed'
    textArea.style.left = '-9999px'
    document.body.appendChild(textArea)
    textArea.focus()
    textArea.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(textArea)
    return ok
  }

  function deleteSegment(taskId: string, index: number) {
    setTasks((prev) =>
      prev.map((task) => {
        if (task.taskId !== taskId) return task
        const segments = task.segments.filter((s) => s.index !== index)
        return {
          ...task,
          segments,
          fullText: segments
            .filter((s) => s.final)
            .map((s) => s.text)
            .join(''),
        }
      }),
    )
  }

  // 字幕时间轴编辑器保存并重新刻印成功后：刷新视频版本号破缓存
  function handleSubtitlesSaved(taskId: string) {
    setTasks((prev) =>
      prev.map((task) =>
        task.taskId === taskId
          ? {
              ...task,
              videoUrl: canonicalSubtitledVideoPath(taskId),
              subtitleVersion: Date.now(),
            }
          : task,
      ),
    )
  }

  async function submitBilibiliUrl() {
    const url = biliUrl.trim()
    if (!url || biliSubmitting || uploadLocked) return
    // 与后端 looks_like_bilibili / looks_like_douyin / looks_like_youtube 保持一致的宽松校验，
    // 同时支持 B 站（BV 号 / bilibili.com / b23.tv）、抖音（douyin.com / v.douyin.com / iesdouyin.com）
    // 与 YouTube（youtube.com / youtu.be / shorts）。
    const isBili = /(BV[0-9A-Za-z]{10})|bilibili\.com\/video\/|b23\.tv\//i.test(url)
    const isDy = /(?:(?:www\.)?douyin\.com\/|(?:www\.)?iesdouyin\.com\/|v\.douyin\.com\/)/i.test(url)
    const isYt = /(?:youtube\.com\/|youtu\.be\/|youtube-nocookie\.com\/)/i.test(url)
    if (!isBili && !isDy && !isYt) {
      setBiliError(t('biliInvalidUrl'))
      return
    }
    setBiliError(null)
    setBiliSubmitting(true)
    logUserClick('upload_bilibili_submit', {
      label: '提交视频链接',
      tab: 'upload',
      meta: { url },
    })
    try {
      const selectedAsrLang = asrLangRef.current
      const queued = await uploadVideoUrl(url, lang, selectedAsrLang)
      if (queued.status !== 'queued' || !queued.task_id) {
        throw new Error(t('biliSubmitError'))
      }
      const fileName = queued.file_name || url
      dispatch({ type: 'toggle-sidebar-open', open: true })
      dispatch({ type: 'toggle-upload-history', open: true })
      resetTimerClocks()
      uploadStartMsRef.current = Date.now()
      setFileNames(fileName)

      const newTask: UploadTaskResult = {
        taskId: queued.task_id,
        fileName,
        fileUrl: appendClientUserQuery(queued.file_url),
        originalFileUrl: queued.original_file_url
          ? appendClientUserQuery(queued.original_file_url)
          : undefined,
        status: 'processing',
        uploadPhase: 'processing',
        uploadPercent: 100,
        message: t('serverProcessing'),
        fullText: '',
        segments: [],
        createdAt: queued.created_at || new Date().toISOString(),
        ...(selectedAsrLang !== 'auto'
          ? {
              detectedLang: selectedAsrLang,
              detectedLangName: t(asrLanguageLabelKey(selectedAsrLang) || selectedAsrLang),
              langForced: true,
            }
          : {}),
      }
      setTasks((prev) => [newTask, ...prev])
      setSelectedTaskId(queued.task_id)

      listeningTaskIdsRef.current.add(queued.task_id)
      listenTask(queued.task_id, fileName, () => {
        listeningTaskIdsRef.current.delete(queued.task_id)
      })
      setBiliUrl('')
    } catch (error) {
      setBiliError(`${t('biliSubmitError')}: ${(error as Error).message}`)
    } finally {
      setBiliSubmitting(false)
    }
  }

  function stageFiles(files: File[]) {
    if (!files.length || uploadLocked) return
    let accepted = files
    if (files.length > MAX_UPLOAD_FILES) {
      accepted = files.slice(0, MAX_UPLOAD_FILES)
      setUploadLimitNotice(t('uploadMaxFiles').replaceAll('{0}', String(MAX_UPLOAD_FILES)))
    } else {
      setUploadLimitNotice(null)
    }
    setPendingFiles(accepted)
  }

  function startPendingUpload() {
    if (!pendingFiles.length || uploadLocked) return
    logUserClick('upload_start_processing', {
      label: t('uploadStartProcess'),
      tab: 'upload',
      fileName: pendingFiles.map((file) => file.name).join(', '),
      meta: { count: pendingFiles.length, asrLanguage: asrLang },
    })
    enqueueFiles(pendingFiles)
    setPendingFiles([])
  }

  function onAsrLangChange(next: AsrLanguageCode) {
    asrLangRef.current = next
    setAsrLang(next)
    saveAsrLanguage(next)
  }

  function onFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || [])
    if (files.length) {
      logUserClick('upload_files_selected', {
        label: '选择音视频文件',
        tab: 'upload',
        fileName: files.map((file) => file.name).join(', '),
        meta: { count: files.length },
      })
    }
    stageFiles(files)
    event.target.value = ''
  }

  const pendingFileNames = pendingFiles.map((file) => file.name).join(', ')
  const displayFileNames = pendingFileNames || fileNames || t('noFileSelected')
  const showFileConfirmRow = pendingFiles.length > 0

  const asrLangOptions = (
    <div className="asr-lang-options" role="group" aria-label={t('asrLangLabel')}>
      <span className="asr-lang-options-label">{t('asrLangLabel')}</span>
      {ASR_LANGUAGE_OPTIONS.map((option) => (
        <button
          type="button"
          key={option.value}
          className={`asr-lang-option${asrLang === option.value ? ' selected' : ''}`}
          aria-pressed={asrLang === option.value}
          disabled={uploadLocked}
          title={t('asrLangHint')}
          data-click-action="select_asr_language"
          data-click-label={t(option.labelKey)}
          data-click-tab="upload"
          onClick={() => onAsrLangChange(option.value)}
        >
          {t(option.labelKey)}
        </button>
      ))}
    </div>
  )

  return (
    <>
      <div
        className={`upload-area${dragOver ? ' drag-over' : ''}${uploadLocked ? ' upload-locked' : ''}`}
        onDragOver={(e) => {
          if (uploadLocked) return
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          if (uploadLocked) return
          const files = Array.from(e.dataTransfer.files || [])
          if (files.length) {
            logUserClick('upload_files_dropped', {
              label: '拖放音视频文件',
              tab: 'upload',
              fileName: files.map((file) => file.name).join(', '),
              meta: { count: files.length },
            })
          }
          stageFiles(files)
        }}
      >
        <input
          type="file"
          id="audio-file-input"
          className="file-input"
          multiple
          disabled={uploadLocked}
          accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus,.amr,.wma,.mp4,.mov,.avi,.mkv,.flv,.webm,.wmv,.3gp,audio/*,video/*"
          onChange={onFileChange}
        />
        <div className="upload-area-main">
          <label
            htmlFor="audio-file-input"
            className={`upload-label${uploadLocked ? ' disabled' : ''}`}
            data-click-action="upload_select_file"
            data-click-label={t('uploadHint')}
            data-click-tab="upload"
          >
            {t('uploadHint')}
          </label>
          <span id="file-name-display">{displayFileNames}</span>
        </div>
        <div className="upload-confirm-row">
          {asrLangOptions}
          {showFileConfirmRow ? (
            <button
              type="button"
              className="upload-start-btn"
              disabled={uploadLocked}
              data-click-action="upload_start_processing"
              data-click-label={t('uploadStartProcess')}
              data-click-tab="upload"
              onClick={startPendingUpload}
            >
              {t('uploadStartProcess')}
            </button>
          ) : null}
        </div>
        <p
          className="upload-multi-hint"
          role={showBackgroundNotice || uploadLimitNotice ? 'status' : undefined}
        >
          {uploadLimitNotice
            ? uploadLimitNotice
            : showBackgroundNotice
              ? t('uploadBackgroundNotice')
              : t('uploadMultiHint').replace('{0}', String(MAX_UPLOAD_FILES))}
        </p>
        <div className="bili-input-row">
          <span className="bili-or-sep">{t('biliOrSeparator')}</span>
          <input
            type="url"
            className="bili-url-input"
            placeholder={t('biliInputPlaceholder')}
            value={biliUrl}
            disabled={uploadLocked || biliSubmitting}
            onChange={(e) => {
              setBiliUrl(e.target.value)
              if (biliError) setBiliError(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                void submitBilibiliUrl()
              }
            }}
          />
          <button
            type="button"
            className="bili-submit-btn"
            disabled={uploadLocked || biliSubmitting || !biliUrl.trim()}
            data-click-action="upload_bilibili_submit"
            data-click-label={t('biliSubmit')}
            data-click-tab="upload"
            onClick={() => void submitBilibiliUrl()}
          >
            {biliSubmitting ? t('biliParsing') : t('biliSubmit')}
          </button>
        </div>
        {biliError ? (
          <p className="bili-error" role="alert">
            {biliError}
          </p>
        ) : null}
      </div>

      <GlobalStatusDashboard
        status={taskDashboard}
        timerText={timerText}
        statusReady={t('statusReady')}
        waitingTask={t('waitingTask')}
      />

      <div className="upload-workspace">
        <UploadTaskDetailPanel
          task={selectedTask}
          playingKey={playingKey}
          showTaskTools={showTaskTools}
          noTaskHint={
            !selectedTask
              ? tasks.length
                ? t('selectTaskHint')
                : ''
              : t('noResult')
          }
          noMediaHint={t('noMediaForTask')}
          videoFallbackHint={t('videoAudioFallback')}
          downloadSubVideoLabel={t('downloadSubVideo')}
          subtitledPendingHint={t('subtitledVideoPending')}
          subtitledPlaybackErrorHint={t('subtitledPlaybackError')}
          subtitleEditable={subtitleEditable}
          onKeywordChange={setTaskKeyword}
          onToggleSpeaker={toggleSpeakerFilter}
          onDeleteSpeaker={deleteSpeaker}
          onCopy={copyVisibleResults}
          onPlay={togglePlay}
          onDeleteSegment={deleteSegment}
          onSubtitlesSaved={handleSubtitlesSaved}
        />
      </div>

      {historySlot && store.ui.uploadHistoryExpanded
        ? createPortal(
            <UploadTaskHistoryPanel
              tasks={tasks}
              selectedTaskId={selectedTaskId}
              title={t('historyTitle')}
              statusProcessing={t('processing')}
              statusPending={t('statusPending')}
              statusDone={t('done')}
              statusError={t('failed')}
              deleteTaskTitle={t('deleteTask')}
              onSelect={setSelectedTaskId}
              onDelete={deleteTask}
              collapseLimit={MAX_UPLOAD_FILES}
            />,
            historySlot,
          )
        : null}
    </>
  )
}
