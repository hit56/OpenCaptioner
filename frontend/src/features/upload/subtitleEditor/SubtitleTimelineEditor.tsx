import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../../../shared/i18n/useI18n'
import {
  fetchSubtitleCues,
  fetchSubtitleCuesDraftVtt,
  saveSubtitleCues,
} from '../../../services/apiClient'
import {
  clamp,
  createCueId,
  cuesEqual,
  MIN_CUE_DURATION,
  toCuePayload,
  toEditableCues,
  type EditableCue,
} from './cueModel'
import { SubtitleTimeline } from './SubtitleTimeline'
import { SubtitleCueList } from './SubtitleCueList'

interface SubtitleTimelineEditorProps {
  taskId: string
  /** 当前播放器的 video 元素（可能因换源而重建）。 */
  videoEl: HTMLVideoElement | null
  /** 时长兜底（秒），一般来自任务媒体时长。 */
  fallbackDuration?: number
  /** 草稿字幕预览轨（Blob VTT URL），传给播放器叠加到原片。 */
  onPreviewVtt: (url: string | null) => void
  /** 保存并重新刻印成功后回调，父组件据此刷新视频版本。 */
  onSaved: () => void
}

function joinText(a: string, b: string): string {
  const left = a.trim()
  const right = b.trim()
  if (!left) return right
  if (!right) return left
  const needsSpace = /[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right)
  return needsSpace ? `${left} ${right}` : `${left}${right}`
}

export function SubtitleTimelineEditor({
  taskId,
  videoEl,
  fallbackDuration,
  onPreviewVtt,
  onSaved,
}: SubtitleTimelineEditorProps) {
  const { t, lang } = useI18n()
  const [cues, setCues] = useState<EditableCue[]>([])
  const [baseline, setBaseline] = useState<EditableCue[]>([])
  const [selectedCueId, setSelectedCueId] = useState<string | null>(null)
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [reportedDuration, setReportedDuration] = useState(0)
  const [videoDuration, setVideoDuration] = useState(0)
  const [saving, setSaving] = useState(false)
  const [statusHint, setStatusHint] = useState('')

  const previewUrlRef = useRef<string | null>(null)

  const setCuesSorted = useCallback((updater: (prev: EditableCue[]) => EditableCue[]) => {
    setCues((prev) => {
      const next = updater(prev)
      return [...next].sort((a, b) => a.start - b.start || a.end - b.end)
    })
  }, [])

  // —— 载入 cue 列表 ——
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(false)
    void (async () => {
      const result = await fetchSubtitleCues(taskId)
      if (cancelled) return
      if (!result) {
        setLoadError(true)
        setLoading(false)
        return
      }
      const editable = toEditableCues(result.cues)
      setCues(editable)
      setBaseline(editable.map((c) => ({ ...c })))
      setReportedDuration(result.duration || 0)
      setSelectedCueId(editable[0]?.id ?? null)
      setLoading(false)
    })()
    return () => {
      cancelled = true
    }
  }, [taskId])

  // —— 绑定 video 元素：播放头/播放状态同步 + 时长 ——
  useEffect(() => {
    if (!videoEl) return
    const syncDuration = () => {
      if (Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
        setVideoDuration(videoEl.duration)
      }
    }
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onTime = () => setCurrentTime(videoEl.currentTime)
    syncDuration()
    setCurrentTime(videoEl.currentTime)
    videoEl.addEventListener('loadedmetadata', syncDuration)
    videoEl.addEventListener('durationchange', syncDuration)
    videoEl.addEventListener('play', onPlay)
    videoEl.addEventListener('playing', onPlay)
    videoEl.addEventListener('pause', onPause)
    videoEl.addEventListener('ended', onPause)
    videoEl.addEventListener('timeupdate', onTime)
    videoEl.addEventListener('seeking', onTime)
    videoEl.addEventListener('seeked', onTime)
    return () => {
      videoEl.removeEventListener('loadedmetadata', syncDuration)
      videoEl.removeEventListener('durationchange', syncDuration)
      videoEl.removeEventListener('play', onPlay)
      videoEl.removeEventListener('playing', onPlay)
      videoEl.removeEventListener('pause', onPause)
      videoEl.removeEventListener('ended', onPause)
      videoEl.removeEventListener('timeupdate', onTime)
      videoEl.removeEventListener('seeking', onTime)
      videoEl.removeEventListener('seeked', onTime)
    }
  }, [videoEl])

  // 播放时用 rAF 平滑推进播放头
  useEffect(() => {
    if (!playing || !videoEl) return
    let raf = 0
    const tick = () => {
      setCurrentTime(videoEl.currentTime)
      raf = window.requestAnimationFrame(tick)
    }
    raf = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(raf)
  }, [playing, videoEl])

  const maxCueEnd = useMemo(
    () => cues.reduce((max, c) => Math.max(max, c.end), 0),
    [cues],
  )
  const duration = Math.max(
    videoDuration,
    reportedDuration,
    maxCueEnd,
    fallbackDuration || 0,
    1,
  )

  const dirty = !cuesEqual(cues, baseline)

  // —— 草稿预览：仅在存在未保存编辑时，把草稿 VTT 叠加到原片；否则清除叠加，
  //    让播放器展示已烧录的字幕成片（刚打开或刚保存完成时即可正常观看视频）。——
  useEffect(() => {
    if (loading || loadError) return
    if (!dirty) {
      if (previewUrlRef.current) {
        URL.revokeObjectURL(previewUrlRef.current)
        previewUrlRef.current = null
      }
      onPreviewVtt(null)
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      const payload = toCuePayload(cues)
      const vtt = await fetchSubtitleCuesDraftVtt(taskId, payload, controller.signal)
      if (controller.signal.aborted) return
      const blobUrl = URL.createObjectURL(new Blob([vtt ?? 'WEBVTT\n\n'], { type: 'text/vtt' }))
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
      previewUrlRef.current = blobUrl
      onPreviewVtt(blobUrl)
    }, 350)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [cues, dirty, loading, loadError, taskId, onPreviewVtt])

  // 卸载时清理预览轨
  useEffect(
    () => () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
      previewUrlRef.current = null
      onPreviewVtt(null)
    },
    [onPreviewVtt],
  )

  const seek = useCallback(
    (time: number, autoPlay = false) => {
      const clamped = clamp(time, 0, duration)
      setCurrentTime(clamped)
      if (videoEl) {
        videoEl.currentTime = clamped
        if (autoPlay) void videoEl.play().catch(() => undefined)
      }
    },
    [duration, videoEl],
  )

  const handleChangeCueTimeLive = useCallback(
    (id: string, start: number, end: number) => {
      setCuesSorted((prev) => prev.map((c) => (c.id === id ? { ...c, start, end } : c)))
    },
    [setCuesSorted],
  )

  const handleEditText = useCallback(
    (id: string, field: 'text' | 'trans', value: string) => {
      setCues((prev) => prev.map((c) => (c.id === id ? { ...c, [field]: value } : c)))
    },
    [],
  )

  const handleEditTime = useCallback(
    (id: string, field: 'start' | 'end', value: number) => {
      setCuesSorted((prev) =>
        prev.map((c) => {
          if (c.id !== id) return c
          if (field === 'start') {
            const start = clamp(value, 0, c.end - MIN_CUE_DURATION)
            return { ...c, start }
          }
          const end = clamp(value, c.start + MIN_CUE_DURATION, duration)
          return { ...c, end }
        }),
      )
    },
    [duration, setCuesSorted],
  )

  const handleSplit = useCallback(
    (id: string) => {
      setCuesSorted((prev) => {
        const idx = prev.findIndex((c) => c.id === id)
        if (idx < 0) return prev
        const cur = prev[idx]
        const withinMargins =
          currentTime > cur.start + MIN_CUE_DURATION && currentTime < cur.end - MIN_CUE_DURATION
        const splitAt = withinMargins ? currentTime : (cur.start + cur.end) / 2
        if (splitAt - cur.start < MIN_CUE_DURATION || cur.end - splitAt < MIN_CUE_DURATION) {
          return prev
        }
        const first: EditableCue = { ...cur, end: splitAt }
        const second: EditableCue = {
          id: createCueId(),
          start: splitAt,
          end: cur.end,
          text: '',
          trans: '',
        }
        const next = [...prev]
        next.splice(idx, 1, first, second)
        return next
      })
    },
    [currentTime, setCuesSorted],
  )

  const handleMergeNext = useCallback(
    (id: string) => {
      setCuesSorted((prev) => {
        const idx = prev.findIndex((c) => c.id === id)
        if (idx < 0 || idx >= prev.length - 1) return prev
        const cur = prev[idx]
        const nxt = prev[idx + 1]
        const merged: EditableCue = {
          ...cur,
          end: Math.max(cur.end, nxt.end),
          text: joinText(cur.text, nxt.text),
          trans: joinText(cur.trans, nxt.trans),
        }
        const next = [...prev]
        next.splice(idx, 2, merged)
        return next
      })
    },
    [setCuesSorted],
  )

  const handleAddAfter = useCallback(
    (id: string) => {
      let newId = ''
      setCuesSorted((prev) => {
        const idx = prev.findIndex((c) => c.id === id)
        if (idx < 0) return prev
        const cur = prev[idx]
        const nxt = prev[idx + 1]
        const boundary = nxt ? nxt.start : duration
        let updatedCur = cur
        let newStart: number
        let newEnd: number
        if (boundary - cur.end >= MIN_CUE_DURATION) {
          newStart = cur.end
          newEnd = Math.min(cur.end + 2, boundary)
        } else {
          const mid = (cur.start + cur.end) / 2
          updatedCur = { ...cur, end: mid }
          newStart = mid
          newEnd = cur.end
        }
        newId = createCueId()
        const created: EditableCue = { id: newId, start: newStart, end: newEnd, text: '', trans: '' }
        const next = [...prev]
        next.splice(idx, 1, updatedCur, created)
        return next
      })
      if (newId) setSelectedCueId(newId)
    },
    [duration, setCuesSorted],
  )

  const handleDelete = useCallback((id: string) => {
    setCues((prev) => prev.filter((c) => c.id !== id))
    setSelectedCueId((prev) => (prev === id ? null : prev))
  }, [])

  const handleAddFirst = useCallback(() => {
    const newId = createCueId()
    const start = clamp(currentTime, 0, Math.max(0, duration - MIN_CUE_DURATION))
    const end = Math.min(start + 2, duration)
    setCuesSorted((prev) => [...prev, { id: newId, start, end, text: '', trans: '' }])
    setSelectedCueId(newId)
  }, [currentTime, duration, setCuesSorted])

  const handleDiscard = useCallback(() => {
    setCues(baseline.map((c) => ({ ...c })))
    setStatusHint('')
  }, [baseline])

  const handleSave = useCallback(async () => {
    const payload = toCuePayload(cues)
    if (!payload.length) {
      setStatusHint(t('subtitleEditorEmpty'))
      return
    }
    setSaving(true)
    setStatusHint(t('reburningHint'))
    try {
      const result = await saveSubtitleCues(taskId, payload, lang)
      if (!result.ok) {
        setStatusHint(result.detail || t('reburnFailed'))
        return
      }
      setBaseline(cues.map((c) => ({ ...c })))
      setStatusHint(t('reburnDone'))
      onSaved()
      window.setTimeout(() => setStatusHint(''), 3000)
    } catch (e) {
      setStatusHint(`${t('reburnFailed')}: ${String((e as Error).message || e)}`)
    } finally {
      setSaving(false)
    }
  }, [cues, lang, onSaved, t, taskId])

  if (loading) {
    return <div className="sub-editor-status">{t('subtitleEditorLoading')}</div>
  }
  if (loadError) {
    return <div className="sub-editor-status error">{t('subtitleEditorLoadError')}</div>
  }

  return (
    <div className="sub-editor">
      <div className="sub-editor-toolbar">
        <span className="sub-editor-title">{t('subtitleEditorTitle')}</span>
        <span className="sub-editor-tip">{t('subtitleEditorTip')}</span>
        <div className="sub-editor-actions">
          <button
            type="button"
            className="sub-editor-btn ghost"
            onClick={handleAddFirst}
            disabled={saving}
          >
            {t('subtitleAddCue')}
          </button>
          <button
            type="button"
            className="sub-editor-btn ghost"
            onClick={handleDiscard}
            disabled={saving || !dirty}
          >
            {t('discardEdits')}
          </button>
          <button
            type="button"
            className="sub-editor-btn primary"
            onClick={handleSave}
            disabled={saving || !dirty}
          >
            {saving ? t('reburning') : t('saveAndReburn')}
          </button>
        </div>
        {statusHint ? <span className="sub-editor-status-hint">{statusHint}</span> : null}
      </div>

      <SubtitleTimeline
        cues={cues}
        duration={duration}
        currentTime={currentTime}
        playing={playing}
        selectedCueId={selectedCueId}
        disabled={saving}
        labels={{
          zoomIn: t('subtitleZoomIn'),
          zoomOut: t('subtitleZoomOut'),
          empty: t('subtitleEditorEmpty'),
        }}
        onSelectCue={setSelectedCueId}
        onSeek={(time) => seek(time)}
        onChangeCueTime={handleChangeCueTimeLive}
      />

      <SubtitleCueList
        cues={cues}
        selectedCueId={selectedCueId}
        disabled={saving}
        labels={{
          original: t('segmentOriginal'),
          translation: t('segmentTranslation'),
          play: t('play'),
          splitCue: t('subtitleSplitCue'),
          mergeNext: t('subtitleMergeNext'),
          addAfter: t('subtitleAddAfter'),
          deleteCue: t('deleteSegment'),
          startTime: t('subtitleStartTime'),
          endTime: t('subtitleEndTime'),
        }}
        onSelectCue={setSelectedCueId}
        onSeekToCue={(cue) => {
          setSelectedCueId(cue.id)
          seek(cue.start, true)
        }}
        onEditText={handleEditText}
        onEditTime={handleEditTime}
        onSplit={handleSplit}
        onMergeNext={handleMergeNext}
        onDelete={handleDelete}
        onAddAfter={handleAddAfter}
      />
    </div>
  )
}
