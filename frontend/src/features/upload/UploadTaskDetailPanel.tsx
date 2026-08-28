import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import type { UploadTaskResult } from '../../shared/types/asr'
import { useI18n } from '../../shared/i18n/useI18n'
import { UploadSearchBar } from './UploadSearchBar'
import { UploadSegmentRow } from './UploadSegmentRow'
import { SpeakerStatsBar } from './SpeakerStatsBar'
import { TaskMediaPlayer } from './TaskMediaPlayer'
import { VideoInsightsPanel } from './videoInsights/VideoInsightsPanel'
import { SubtitleTimelineEditor } from './subtitleEditor/SubtitleTimelineEditor'
import {
  buildPublishCoverDownloadUrl,
  buildPublishCoverUrl,
  buildSubtitledExportUrl,
  resolveSubtitledDownloadUrl,
  resolveTaskDisplayMessage,
  resolveTaskHistoryStatus,
  resolveTaskMediaDurationSeconds,
} from './taskMedia'
import { segmentMatchesFilters } from './segmentVisibility'
import { segmentHasDistinctTranslation } from './segmentTranslations'
import { mergeSpeakerStats, type SpeakerStatItem } from './speakerStatsUtils'
import {
  fetchBilibiliPublishMeta,
  generateBilibiliPublishCover,
  saveBilibiliPublishMeta,
} from '../../services/apiClient'

interface UploadTaskDetailPanelProps {
  task: UploadTaskResult | undefined
  playingKey: string | null
  showTaskTools: boolean
  noTaskHint: string
  noMediaHint: string
  videoFallbackHint: string
  downloadSubVideoLabel: string
  subtitledPendingHint: string
  subtitledPlaybackErrorHint: string
  /** 是否允许编辑字幕（仅对已完成的视频任务开放） */
  subtitleEditable: boolean
  onKeywordChange: (value: string) => void
  onToggleSpeaker: (speakerId: string) => void
  onDeleteSpeaker: (speakerId: string) => void
  onCopy: (withTime: boolean) => boolean
  onPlay: (key: string, timestamp: string, audioUrl: string) => void
  onDeleteSegment: (taskId: string, index: number) => void
  /** 字幕保存并重新刻印成功后回调，父组件据此刷新视频版本号破缓存 */
  onSubtitlesSaved: (taskId: string) => void
}

type PublishPlatform = 'bilibili' | 'xiaohongshu' | 'douyin' | 'youtube'
type CoverKind = 'generated' | 'frame_start' | 'frame_middle' | 'frame_end'
type FrameCoverKind = 'frame_start' | 'frame_middle' | 'frame_end'
type CoverSource = 'generated' | 'frame'

const COVER_KIND_LIST: CoverKind[] = ['generated', 'frame_start', 'frame_middle', 'frame_end']
const FRAME_COVER_KINDS: FrameCoverKind[] = ['frame_start', 'frame_middle', 'frame_end']

type CoverSlotState = {
  url: string
  loading: boolean
  error: string
}

type PublishUiState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | {
      phase: 'form'
      title: string
      desc: string
      tags: string
      confirmMenuOpen?: boolean
      needTitle?: boolean
      openedHint?: boolean
      fromCache?: boolean
      covers: Record<CoverKind, CoverSlotState>
      coverSelected: CoverKind
      coverVersion?: number
    }
  | { phase: 'error'; message: string }

const PUBLISH_PLATFORMS: Record<PublishPlatform, { url: string; labelKey: string }> = {
  bilibili: {
    url: 'https://member.bilibili.com/platform/upload/video/frame',
    labelKey: 'publishToBilibili',
  },
  xiaohongshu: {
    url: 'https://creator.xiaohongshu.com/publish/publish',
    labelKey: 'publishToXiaohongshu',
  },
  douyin: {
    url: 'https://creator.douyin.com/creator-micro/content/upload',
    labelKey: 'publishToDouyin',
  },
  youtube: {
    url: 'https://www.youtube.com/upload',
    labelKey: 'publishToYoutube',
  },
}

function defaultPublishTitle(fileName: string): string {
  const base = fileName.replace(/\.[^/.]+$/, '').trim() || '字幕视频'
  return base.replace(/(_subtitle|_subtitled|_字幕)$/i, '').trim() || base
}

function coverSlotFromAvailable(
  taskId: string,
  kind: CoverKind,
  available: boolean,
  version: number,
): CoverSlotState {
  return {
    url: available ? buildPublishCoverUrl(taskId, version, kind) : '',
    loading: !available,
    error: '',
  }
}

function isFrameCover(kind: CoverKind): kind is FrameCoverKind {
  return kind !== 'generated'
}

function framesBusy(covers: Record<CoverKind, CoverSlotState>): boolean {
  return FRAME_COVER_KINDS.some((kind) => covers[kind].loading)
}

function coverLabelKey(kind: CoverKind): string {
  if (kind === 'generated') return 'publishCoverGenerated'
  if (kind === 'frame_start') return 'publishCoverFrameStart'
  if (kind === 'frame_middle') return 'publishCoverFrameMiddle'
  return 'publishCoverFrameEnd'
}

export function UploadTaskDetailPanel({
  task,
  playingKey,
  showTaskTools,
  noTaskHint,
  noMediaHint,
  videoFallbackHint,
  downloadSubVideoLabel,
  subtitledPendingHint,
  subtitledPlaybackErrorHint,
  subtitleEditable,
  onKeywordChange,
  onToggleSpeaker,
  onDeleteSpeaker,
  onCopy,
  onPlay,
  onDeleteSegment,
  onSubtitlesSaved,
}: UploadTaskDetailPanelProps) {
  const { t, lang } = useI18n()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [draftPreviewVttUrl, setDraftPreviewVttUrl] = useState<string | null>(null)
  const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null)
  const [publishState, setPublishState] = useState<PublishUiState>({ phase: 'idle' })
  const [copiedPublishField, setCopiedPublishField] = useState<'title' | 'desc' | 'tags' | null>(
    null,
  )
  const metaRequestSeq = useRef(0)
  const publishConfirmMenuRef = useRef<HTMLDivElement>(null)
  const publishSaveTimerRef = useRef<number | null>(null)
  const copiedPublishTimerRef = useRef<number | null>(null)
  const taskId = task?.taskId

  useEffect(() => {
    setEditorOpen(false)
    setDraftPreviewVttUrl(null)
    setPublishState({ phase: 'idle' })
    setCopiedPublishField(null)
    metaRequestSeq.current += 1
    if (publishSaveTimerRef.current != null) {
      window.clearTimeout(publishSaveTimerRef.current)
      publishSaveTimerRef.current = null
    }
    if (copiedPublishTimerRef.current != null) {
      window.clearTimeout(copiedPublishTimerRef.current)
      copiedPublishTimerRef.current = null
    }
  }, [taskId])

  useEffect(() => {
    return () => {
      if (publishSaveTimerRef.current != null) {
        window.clearTimeout(publishSaveTimerRef.current)
        publishSaveTimerRef.current = null
      }
      if (copiedPublishTimerRef.current != null) {
        window.clearTimeout(copiedPublishTimerRef.current)
        copiedPublishTimerRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (publishState.phase !== 'form' || !publishState.confirmMenuOpen) return
    const onPointerDown = (event: MouseEvent) => {
      const root = publishConfirmMenuRef.current
      if (root && !root.contains(event.target as Node)) {
        setPublishState((prev) =>
          prev.phase === 'form' ? { ...prev, confirmMenuOpen: false } : prev,
        )
      }
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [publishState])

  const handleSaved = useCallback(() => {
    if (taskId) onSubtitlesSaved(taskId)
  }, [onSubtitlesSaved, taskId])

  const persistPublishMeta = useCallback(
    async (payload: {
      title: string
      desc: string
      tags: string
      cover_kind?: CoverKind
    }) => {
      if (!task) return
      const title = payload.title.trim()
      if (!title) return
      try {
        await saveBilibiliPublishMeta(task.taskId, {
          title,
          desc: payload.desc,
          tags: payload.tags,
          cover_kind: payload.cover_kind,
        })
      } catch {
        // 保存失败不打断投稿流程，下次仍可再试
      }
    },
    [task],
  )

  const schedulePersistPublishMeta = useCallback(
    (payload: {
      title: string
      desc: string
      tags: string
      cover_kind?: CoverKind
    }) => {
      if (publishSaveTimerRef.current != null) {
        window.clearTimeout(publishSaveTimerRef.current)
      }
      publishSaveTimerRef.current = window.setTimeout(() => {
        void persistPublishMeta(payload)
      }, 600)
    },
    [persistPublishMeta],
  )

  const closePublishPanel = useCallback(() => {
    setPublishState((prev) => {
      if (prev.phase === 'form') {
        void persistPublishMeta({
          title: prev.title,
          desc: prev.desc,
          tags: prev.tags,
          cover_kind: prev.coverSelected,
        })
      }
      return { phase: 'idle' }
    })
  }, [persistPublishMeta])

  const requestCover = useCallback(
    async (
      source: CoverSource,
      payload: { title: string; desc: string; tags: string },
      refresh: boolean,
      seq: number,
    ) => {
      if (!task) return
      try {
        const result = await generateBilibiliPublishCover(task.taskId, {
          ...payload,
          refresh,
          source,
        })
        if (seq !== metaRequestSeq.current) return
        const version = Date.now()
        setPublishState((prev) => {
          if (prev.phase !== 'form') return prev
          const covers = { ...prev.covers }
          if (source === 'generated') {
            covers.generated = {
              url: buildPublishCoverUrl(task.taskId, version, 'generated'),
              loading: false,
              error: '',
            }
          } else {
            for (const kind of FRAME_COVER_KINDS) {
              const available = Boolean(result.cover_frames_available?.[kind] ?? true)
              covers[kind] = {
                url: available ? buildPublishCoverUrl(task.taskId, version, kind) : '',
                loading: false,
                error: available ? '' : t('publishCoverFrameFailed'),
              }
            }
          }
          const selectedSlot = covers[prev.coverSelected]
          let coverSelected = prev.coverSelected
          if (!selectedSlot.url && !selectedSlot.loading) {
            coverSelected =
              COVER_KIND_LIST.find((kind) => covers[kind].url) || prev.coverSelected
          }
          return { ...prev, covers, coverSelected, coverVersion: version }
        })
      } catch (coverErr) {
        if (seq !== metaRequestSeq.current) return
        const error =
          coverErr instanceof Error
            ? coverErr.message
            : t(source === 'frame' ? 'publishCoverFrameFailed' : 'publishCoverFailed')
        setPublishState((prev) => {
          if (prev.phase !== 'form') return prev
          const covers = { ...prev.covers }
          const failedKinds: CoverKind[] = source === 'generated' ? ['generated'] : FRAME_COVER_KINDS
          for (const kind of failedKinds) {
            covers[kind] = { ...covers[kind], loading: false, error }
          }
          const selectedSlot = covers[prev.coverSelected]
          let coverSelected = prev.coverSelected
          if (!selectedSlot.url && !selectedSlot.loading) {
            coverSelected =
              COVER_KIND_LIST.find((kind) => covers[kind].url) || prev.coverSelected
          }
          return { ...prev, covers, coverSelected }
        })
      }
    },
    [t, task],
  )

  const openPublishForm = useCallback(
    async (refresh = false) => {
      if (!task) return
      const seq = ++metaRequestSeq.current
      setPublishState({ phase: 'loading' })
      try {
        const meta = await fetchBilibiliPublishMeta(task.taskId, { refresh })
        if (seq !== metaRequestSeq.current) return
        const title = meta.title || defaultPublishTitle(task.fileName)
        const desc = meta.desc || ''
        const tags = meta.tags || ''
        const version = Date.now()
        const hasGenerated = Boolean(meta.cover_generated_available)
        const framesAvail = meta.cover_frames_available || {}
        const hasAllFrames = FRAME_COVER_KINDS.every((kind) => framesAvail[kind])
        const hasAnyFrame = FRAME_COVER_KINDS.some((kind) => framesAvail[kind])
        const coverSelected: CoverKind =
          meta.cover_selected &&
          (meta.cover_selected === 'generated'
            ? hasGenerated
            : isFrameCover(meta.cover_selected) && Boolean(framesAvail[meta.cover_selected]))
            ? meta.cover_selected
            : hasGenerated
              ? 'generated'
              : hasAnyFrame
                ? FRAME_COVER_KINDS.find((kind) => framesAvail[kind]) || 'generated'
                : 'generated'
        setPublishState({
          phase: 'form',
          title,
          desc,
          tags,
          fromCache: Boolean(meta.cached) && !refresh,
          covers: {
            generated: coverSlotFromAvailable(task.taskId, 'generated', hasGenerated, version),
            frame_start: coverSlotFromAvailable(
              task.taskId,
              'frame_start',
              Boolean(framesAvail.frame_start),
              version,
            ),
            frame_middle: coverSlotFromAvailable(
              task.taskId,
              'frame_middle',
              Boolean(framesAvail.frame_middle),
              version,
            ),
            frame_end: coverSlotFromAvailable(
              task.taskId,
              'frame_end',
              Boolean(framesAvail.frame_end),
              version,
            ),
          },
          coverSelected,
          coverVersion: version,
        })
        const payload = { title, desc, tags }
        const jobs: Promise<void>[] = []
        if (!hasGenerated) jobs.push(requestCover('generated', payload, false, seq))
        if (!hasAllFrames) jobs.push(requestCover('frame', payload, false, seq))
        if (jobs.length) await Promise.all(jobs)
      } catch (err) {
        if (seq !== metaRequestSeq.current) return
        setPublishState({
          phase: 'error',
          message: err instanceof Error ? err.message : t('publishMetaFailed'),
        })
      }
    },
    [requestCover, t, task],
  )

  const togglePublishPanel = useCallback(() => {
    if (publishState.phase === 'loading') return
    if (publishState.phase === 'form') {
      void persistPublishMeta({
        title: publishState.title,
        desc: publishState.desc,
        tags: publishState.tags,
        cover_kind: publishState.coverSelected,
      })
      setPublishState({ phase: 'idle' })
      return
    }
    if (publishState.phase === 'error') {
      setPublishState({ phase: 'idle' })
      return
    }
    void openPublishForm(false)
  }, [openPublishForm, persistPublishMeta, publishState])

  const regenerateCover = useCallback(async () => {
    if (!task || publishState.phase !== 'form') return
    const seq = metaRequestSeq.current
    const payload = {
      title: publishState.title,
      desc: publishState.desc,
      tags: publishState.tags,
    }
    setPublishState({
      ...publishState,
      covers: {
        ...publishState.covers,
        generated: { ...publishState.covers.generated, loading: true, error: '' },
      },
    })
    await requestCover('generated', payload, true, seq)
  }, [requestCover, publishState, task])

  const reextractFrames = useCallback(async () => {
    if (!task || publishState.phase !== 'form') return
    const seq = metaRequestSeq.current
    setPublishState({
      ...publishState,
      covers: {
        ...publishState.covers,
        frame_start: { ...publishState.covers.frame_start, loading: true, error: '' },
        frame_middle: { ...publishState.covers.frame_middle, loading: true, error: '' },
        frame_end: { ...publishState.covers.frame_end, loading: true, error: '' },
      },
    })
    await requestCover(
      'frame',
      {
        title: publishState.title,
        desc: publishState.desc,
        tags: publishState.tags,
      },
      true,
      seq,
    )
  }, [requestCover, publishState, task])

  const selectCover = useCallback(
    (kind: CoverKind) => {
      if (publishState.phase !== 'form') return
      const slot = publishState.covers[kind]
      if (!slot.url || slot.loading) return
      setPublishState({ ...publishState, coverSelected: kind })
      void persistPublishMeta({
        title: publishState.title,
        desc: publishState.desc,
        tags: publishState.tags,
        cover_kind: kind,
      })
    },
    [persistPublishMeta, publishState],
  )

  const toggleConfirmMenu = useCallback(() => {
    setPublishState((prev) => {
      if (prev.phase !== 'form') return prev
      return { ...prev, confirmMenuOpen: !prev.confirmMenuOpen }
    })
  }, [])

  const submitPublish = useCallback(
    (platform: PublishPlatform) => {
      if (!task || publishState.phase !== 'form') return
      const title = publishState.title.trim()
      if (!title) {
        setPublishState({
          ...publishState,
          confirmMenuOpen: false,
          needTitle: true,
        })
        return
      }
      const target = PUBLISH_PLATFORMS[platform]
      window.open(target.url, '_blank', 'noopener,noreferrer')
      void persistPublishMeta({
        title,
        desc: publishState.desc,
        tags: publishState.tags,
        cover_kind: publishState.coverSelected,
      })
      // 保持表单展开，方便用户回来复制标题/简介/标签
      setPublishState({
        ...publishState,
        confirmMenuOpen: false,
        needTitle: false,
        openedHint: true,
      })
    },
    [task, publishState, persistPublishMeta],
  )

  const updatePublishField = useCallback(
    (field: 'title' | 'desc' | 'tags', value: string) => {
      setPublishState((prev) => {
        if (prev.phase !== 'form') return prev
        const next = {
          ...prev,
          [field]: value,
          openedHint: prev.openedHint,
          needTitle: field === 'title' ? false : prev.needTitle,
        }
        schedulePersistPublishMeta({
          title: next.title,
          desc: next.desc,
          tags: next.tags,
          cover_kind: next.coverSelected,
        })
        return next
      })
    },
    [schedulePersistPublishMeta],
  )

  const copyPublishField = useCallback(
    async (field: 'title' | 'desc' | 'tags', value: string, event?: ReactMouseEvent) => {
      event?.preventDefault()
      event?.stopPropagation()
      const text = value.trim()
      if (!text) return

      let ok = false
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(text)
          ok = true
        } catch {
          ok = false
        }
      }
      if (!ok) {
        const textArea = document.createElement('textarea')
        textArea.value = text
        textArea.setAttribute('readonly', '')
        textArea.style.position = 'fixed'
        textArea.style.left = '-9999px'
        document.body.appendChild(textArea)
        textArea.focus()
        textArea.select()
        try {
          ok = document.execCommand('copy')
        } catch {
          ok = false
        }
        document.body.removeChild(textArea)
      }
      if (!ok) return

      setCopiedPublishField(field)
      if (copiedPublishTimerRef.current != null) {
        window.clearTimeout(copiedPublishTimerRef.current)
      }
      copiedPublishTimerRef.current = window.setTimeout(() => {
        setCopiedPublishField(null)
        copiedPublishTimerRef.current = null
      }, 1500)
    },
    [],
  )

  const keyword = task?.keyword ?? ''
  const speakerFilter = task?.speakerFilter ?? null
  const hasActiveFilters = keyword.trim().length > 0 || speakerFilter !== null
  const segmentCount = task?.segments.length ?? 0

  const speakerStats = useMemo((): SpeakerStatItem[] => {
    if (!task) return []
    return mergeSpeakerStats(task.speakerStats ?? [], task.segments)
  }, [task])

  useEffect(() => {
    if (!task || !scrollRef.current) return
    const el = scrollRef.current
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
      el.scrollTop = el.scrollHeight
    }
  }, [segmentCount, task])

  if (!task) {
    if (noTaskHint) {
      return (
        <div className="upload-detail-panel">
          <div className="no-result-hint">{noTaskHint}</div>
        </div>
      )
    }

    return (
      <div className="upload-detail-panel">
        <div className="upload-empty-guide" role="region" aria-label={t('emptyGuideTitle')}>
          <div className="upload-empty-guide-hero">
            <div className="upload-empty-guide-icon" aria-hidden="true">
              <svg viewBox="0 0 48 48" width="40" height="40" fill="none">
                <rect x="6" y="10" width="36" height="28" rx="4" stroke="currentColor" strokeWidth="2" />
                <path
                  d="M18 20h12M18 26h8"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
                <circle cx="34" cy="30" r="5" stroke="currentColor" strokeWidth="2" />
                <path
                  d="M34 28v4M32 30h4"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </div>
            <h3 className="upload-empty-guide-title">{t('emptyGuideTitle')}</h3>
            <p className="upload-empty-guide-desc">{t('emptyGuideDesc')}</p>
          </div>

          <div className="upload-empty-guide-sections">
            <section className="upload-empty-guide-section">
              <h4 className="upload-empty-guide-section-title">{t('emptyGuideHowTitle')}</h4>
              <ol className="upload-empty-guide-list">
                <li>{t('emptyGuideHow1')}</li>
                <li>{t('emptyGuideHow2')}</li>
                <li>{t('emptyGuideHow3')}</li>
              </ol>
            </section>

            <section className="upload-empty-guide-section">
              <h4 className="upload-empty-guide-section-title">{t('emptyGuideTipsTitle')}</h4>
              <ul className="upload-empty-guide-tips">
                <li>{t('emptyGuideTip1')}</li>
                <li>{t('emptyGuideTip2')}</li>
                <li>{t('emptyGuideTip3')}</li>
                <li>{t('emptyGuideTip4')}</li>
              </ul>
            </section>
          </div>
        </div>
      </div>
    )
  }

  const visibleSegments = task.segments.filter((segment) =>
    segmentMatchesFilters(segment, keyword, speakerFilter),
  )
  const blockVisible = !hasActiveFilters || visibleSegments.length > 0
  const subtitledDownloadUrl = resolveSubtitledDownloadUrl(task)
  const hasSegments = task.segments.length > 0 || Boolean(task.fullText?.trim())
  const subtitleExportUrl = hasSegments ? buildSubtitledExportUrl(task.taskId, lang) : ''
  const showBilingualHeader = task.segments.some((segment) => segmentHasDistinctTranslation(segment))
  const displayStatus = resolveTaskHistoryStatus(task)
  const displayMessage = resolveTaskDisplayMessage(task, {
    done: t('done'),
    processing: t('processing'),
  })
  const showCopyButton =
    displayStatus === 'done' && (task.segments.length > 0 || Boolean(task.fullText?.trim()))
  const publishBusy = publishState.phase === 'loading'
  const publishPanelOpen =
    publishState.phase === 'loading' ||
    publishState.phase === 'form' ||
    publishState.phase === 'error'

  return (
    <div className="upload-detail-panel">
      <div className="upload-detail-header">
        <span className="upload-detail-title" title={task.fileName}>
          {task.fileName}
        </span>
        <span className={`status-indicator${displayStatus === 'done' ? ' done' : ''}`}>
          {displayMessage}
        </span>
        {subtitledDownloadUrl ? (
          <a
            className="header-download-btn"
            href={subtitledDownloadUrl}
            download={`${task.fileName.replace(/\.[^/.]+$/, '')}_subtitle.mp4`}
            target="_blank"
            rel="noreferrer"
            data-click-action="download_subtitled_video"
            data-click-label={downloadSubVideoLabel}
            data-click-tab="upload"
            data-task-id={task.taskId}
            data-file-name={task.fileName}
          >
            {downloadSubVideoLabel}
          </a>
        ) : null}
        {subtitledDownloadUrl ? (
          <button
            type="button"
            className="header-download-btn publish-bili-btn"
            disabled={publishBusy}
            aria-expanded={publishPanelOpen}
            data-click-action="publish_panel_toggle"
            data-click-label={t('publishMenuButton')}
            data-click-tab="upload"
            data-task-id={task.taskId}
            data-file-name={task.fileName}
            onClick={togglePublishPanel}
          >
            {publishBusy ? t('publishMetaGenerating') : t('publishMenuButton')}
          </button>
        ) : null}
        {subtitleExportUrl ? (
          <a
            className="header-download-btn export-srt-btn"
            href={subtitleExportUrl}
            download={`${task.fileName.replace(/\.[^/.]+$/, '')}.srt`}
            target="_blank"
            rel="noreferrer"
            data-click-action="download_subtitle_srt"
            data-click-label={t('exportSrt')}
            data-click-tab="upload"
            data-task-id={task.taskId}
            data-file-name={task.fileName}
          >
            {t('exportSrt')}
          </a>
        ) : null}
      </div>

      {publishPanelOpen ? (
        <div className="bili-publish-panel" role="region" aria-label={t('publishMenuTitle')}>
          <div className="bili-publish-panel-title">{t('publishMenuTitle')}</div>
          {publishState.phase === 'loading' ? (
            <div className="bili-publish-status">
              <div className="bili-publish-status-msg">{t('publishMetaGenerating')}</div>
            </div>
          ) : null}
          {publishState.phase === 'form' ? (
            <>
              {publishState.openedHint ? (
                <p className="bili-publish-hint">{t('publishOpenedKeepForm')}</p>
              ) : publishState.needTitle ? (
                <p className="bili-publish-hint error">{t('publishNeedTitle')}</p>
              ) : publishState.fromCache ? (
                <p className="bili-publish-hint">{t('publishMetaFromCache')}</p>
              ) : null}
              <div className="bili-publish-field">
                <div className="bili-publish-field-label">
                  <span>{t('publishTitleLabel')}</span>
                  <button
                    type="button"
                    className="bili-publish-copy-btn"
                    onClick={(e) => void copyPublishField('title', publishState.title, e)}
                  >
                    {copiedPublishField === 'title' ? t('copied') : t('publishCopy')}
                  </button>
                </div>
                <input
                  type="text"
                  maxLength={80}
                  value={publishState.title}
                  onChange={(e) => updatePublishField('title', e.target.value)}
                />
              </div>
              <div className="bili-publish-field">
                <div className="bili-publish-field-label">
                  <span>{t('publishDescLabel')}</span>
                  <button
                    type="button"
                    className="bili-publish-copy-btn"
                    onClick={(e) => void copyPublishField('desc', publishState.desc, e)}
                  >
                    {copiedPublishField === 'desc' ? t('copied') : t('publishCopy')}
                  </button>
                </div>
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={publishState.desc}
                  onChange={(e) => updatePublishField('desc', e.target.value)}
                />
              </div>
              <div className="bili-publish-field">
                <div className="bili-publish-field-label">
                  <span>{t('publishTagsLabel')}</span>
                  <button
                    type="button"
                    className="bili-publish-copy-btn"
                    onClick={(e) => void copyPublishField('tags', publishState.tags, e)}
                  >
                    {copiedPublishField === 'tags' ? t('copied') : t('publishCopy')}
                  </button>
                </div>
                <input
                  type="text"
                  maxLength={200}
                  value={publishState.tags}
                  placeholder="#字幕 #双语字幕 #AI字幕"
                  onChange={(e) => updatePublishField('tags', e.target.value)}
                />
              </div>
              <div className="bili-publish-cover">
                <div className="bili-publish-field-label">
                  <span>{t('publishCoverLabel')}</span>
                  <div className="bili-publish-cover-actions">
                    {publishState.covers[publishState.coverSelected].url ? (
                      <a
                        className="bili-publish-copy-btn"
                        href={buildPublishCoverDownloadUrl(task.taskId, publishState.coverSelected)}
                        download={`${task.fileName.replace(/\.[^/.]+$/, '')}_cover.${
                          isFrameCover(publishState.coverSelected) ? 'jpg' : 'png'
                        }`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {t('publishCoverDownload')}
                      </a>
                    ) : null}
                    <button
                      type="button"
                      className="bili-publish-copy-btn"
                      disabled={publishState.covers.generated.loading}
                      onClick={() => void regenerateCover()}
                    >
                      {publishState.covers.generated.loading
                        ? t('publishCoverGenerating')
                        : t('publishCoverRegenerate')}
                    </button>
                    <button
                      type="button"
                      className="bili-publish-copy-btn"
                      disabled={framesBusy(publishState.covers)}
                      onClick={() => void reextractFrames()}
                    >
                      {framesBusy(publishState.covers)
                        ? t('publishCoverExtractingFrames')
                        : t('publishCoverReextractFrames')}
                    </button>
                  </div>
                </div>
                <div className="bili-publish-cover-grid">
                  {COVER_KIND_LIST.map((kind) => {
                    const slot = publishState.covers[kind]
                    const selected = publishState.coverSelected === kind
                    const label = t(coverLabelKey(kind))
                    const loadingText = isFrameCover(kind)
                      ? t('publishCoverExtractingFrames')
                      : t('publishCoverGenerating')
                    return (
                      <button
                        key={kind}
                        type="button"
                        className={`bili-publish-cover-card${selected ? ' selected' : ''}`}
                        disabled={!slot.url || slot.loading}
                        aria-pressed={selected}
                        onClick={() => selectCover(kind)}
                      >
                        <div className="bili-publish-cover-card-head">
                          <span>{label}</span>
                          {selected && slot.url ? (
                            <span className="bili-publish-cover-badge">{t('publishCoverSelected')}</span>
                          ) : null}
                        </div>
                        {slot.loading ? (
                          <div className="bili-publish-cover-placeholder">{loadingText}</div>
                        ) : slot.url ? (
                          <img
                            className="bili-publish-cover-img"
                            src={slot.url}
                            alt={label}
                          />
                        ) : (
                          <div className="bili-publish-cover-placeholder error">
                            {slot.error || t('publishCoverEmpty')}
                          </div>
                        )}
                        {slot.error && slot.url ? (
                          <div className="bili-publish-cover-error">{slot.error}</div>
                        ) : null}
                      </button>
                    )
                  })}
                </div>
              </div>
              <div className="bili-publish-actions">
                <div className="publish-confirm-wrap" ref={publishConfirmMenuRef}>
                  <button
                    type="button"
                    className="bili-publish-primary"
                    aria-expanded={Boolean(publishState.confirmMenuOpen)}
                    aria-haspopup="menu"
                    data-click-action="publish_confirm_menu"
                    data-click-label={t('publishConfirm')}
                    data-click-tab="upload"
                    data-task-id={task.taskId}
                    onClick={toggleConfirmMenu}
                  >
                    {t('publishConfirm')}
                  </button>
                  {publishState.confirmMenuOpen ? (
                    <div className="publish-platform-menu publish-confirm-menu" role="menu">
                      {(Object.keys(PUBLISH_PLATFORMS) as PublishPlatform[]).map((platform) => (
                        <button
                          key={platform}
                          type="button"
                          role="menuitem"
                          className="publish-platform-menu-item"
                          data-click-action={`publish_${platform}_confirm`}
                          data-click-label={t(PUBLISH_PLATFORMS[platform].labelKey)}
                          data-click-tab="upload"
                          data-task-id={task.taskId}
                          onClick={() => submitPublish(platform)}
                        >
                          {t(PUBLISH_PLATFORMS[platform].labelKey)}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <button
                  type="button"
                  className="bili-publish-secondary"
                  data-click-action="publish_regenerate"
                  data-click-label={t('publishRegenerate')}
                  data-click-tab="upload"
                  data-task-id={task.taskId}
                  onClick={() => void openPublishForm(true)}
                >
                  {t('publishRegenerate')}
                </button>
                <button
                  type="button"
                  className="bili-publish-secondary"
                  onClick={closePublishPanel}
                >
                  {t('publishCancel')}
                </button>
              </div>
            </>
          ) : null}
          {publishState.phase === 'error' ? (
            <div className="bili-publish-status error">
              <div className="bili-publish-status-msg">{publishState.message}</div>
              <div className="bili-publish-actions">
                <button
                  type="button"
                  className="bili-publish-primary"
                  onClick={() => void openPublishForm(false)}
                >
                  {t('publishRetry')}
                </button>
                <button
                  type="button"
                  className="bili-publish-secondary"
                  onClick={() => setPublishState({ phase: 'idle' })}
                >
                  {t('publishCancel')}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {subtitleEditable ? (
        <div className="subtitle-edit-bar">
          <span className="subtitle-edit-hint">{t('subtitleEditorBarHint')}</span>
          <div className="subtitle-edit-actions">
            <button
              type="button"
              className={`subtitle-reburn-btn${editorOpen ? ' active' : ''}`}
              data-click-action="upload_subtitle_toggle_editor"
              data-click-label={t('subtitleEditorOpen')}
              data-click-tab="upload"
              data-task-id={task.taskId}
              onClick={() => setEditorOpen((prev) => !prev)}
            >
              {editorOpen ? t('subtitleEditorClose') : t('subtitleEditorOpen')}
            </button>
          </div>
        </div>
      ) : null}

      <TaskMediaPlayer
        task={task}
        noMediaLabel={noMediaHint}
        videoFallbackHint={videoFallbackHint}
        subtitledPendingHint={subtitledPendingHint}
        subtitledPlaybackErrorHint={subtitledPlaybackErrorHint}
        previewTrackLabel={t('subtitlePreviewTrackLabel')}
        draftPreviewVttUrl={editorOpen ? draftPreviewVttUrl : null}
        onVideoElement={setVideoEl}
      />

      {subtitleEditable && editorOpen ? (
        <SubtitleTimelineEditor
          key={task.taskId}
          taskId={task.taskId}
          videoEl={videoEl}
          fallbackDuration={resolveTaskMediaDurationSeconds(task) ?? undefined}
          onPreviewVtt={setDraftPreviewVttUrl}
          onSaved={handleSaved}
        />
      ) : null}

      {showTaskTools && speakerStats.length > 0 ? (
        <div className="upload-detail-speakers">
          <SpeakerStatsBar
            speakers={speakerStats}
            activeSpeakerId={speakerFilter}
            detectedLang={task.detectedLang}
            detectedLangName={task.detectedLangName}
            onToggleSpeaker={onToggleSpeaker}
            onDeleteSpeaker={onDeleteSpeaker}
          />
        </div>
      ) : null}

      {showTaskTools ? (
        <div className="upload-detail-search">
          <UploadSearchBar
            keyword={keyword}
            showCopyButton={showCopyButton}
            onKeywordChange={onKeywordChange}
            onCopy={onCopy}
          />
        </div>
      ) : null}

      <div
        id="result-box-upload"
        className="result-box upload-detail-results single-mode-wrapper"
      >
        <div id="result-content-upload" className="result-scroll-area" ref={scrollRef}>
          {!task.segments.length && !task.fullText ? (
            <div className="no-result-hint">{noTaskHint}</div>
          ) : null}
          <div
            className="file-result-block single-file-mode"
            style={{ display: blockVisible ? 'flex' : 'none' }}
          >
            <div className={`file-content-list${task.segments.length ? ' has-content' : ''}`}>
              {showBilingualHeader ? (
                <div className="segment-bilingual-header">
                  <div className="segment-bilingual-header-avatar" aria-hidden="true" />
                  <div className="segment-bilingual-header-meta" aria-hidden="true" />
                  <div className="segment-bilingual has-translation">
                    <span className="segment-column-label">{t('segmentOriginal')}</span>
                    <span className="segment-column-label segment-column-label-translation">
                      {t('segmentTranslation')}
                    </span>
                  </div>
                </div>
              ) : null}
              {task.segments.map((segment) => (
                <UploadSegmentRow
                  key={`${task.taskId}-${segment.index}`}
                  segment={segment}
                  masterAudioUrl={task.fileUrl}
                  playingKey={playingKey}
                  playKey={`${task.taskId}-${segment.index}`}
                  keyword={keyword}
                  visible={segmentMatchesFilters(segment, keyword, speakerFilter)}
                  editable={false}
                  onPlay={onPlay}
                  onDelete={() => onDeleteSegment(task.taskId, segment.index)}
                />
              ))}
              {!task.segments.length && task.fullText ? (
                <div className="segment-row seg-final">
                  <div className="segment-text">{task.fullText}</div>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <VideoInsightsPanel task={task} />
    </div>
  )
}
