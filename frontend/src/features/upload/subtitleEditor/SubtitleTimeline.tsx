import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { clamp, formatShortTime, MIN_CUE_DURATION, type EditableCue } from './cueModel'

interface SubtitleTimelineProps {
  cues: EditableCue[]
  duration: number
  currentTime: number
  playing: boolean
  selectedCueId: string | null
  disabled?: boolean
  labels: {
    zoomIn: string
    zoomOut: string
    empty: string
  }
  onSelectCue: (id: string) => void
  onSeek: (time: number) => void
  /** 拖拽/缩放过程中实时回传新的起止时间（不重排序，避免抖动）。 */
  onChangeCueTime: (id: string, start: number, end: number) => void
}

type DragMode = 'move' | 'resize-start' | 'resize-end'

interface DragState {
  id: string
  mode: DragMode
  startClientX: number
  origStart: number
  origEnd: number
  prevEnd: number
  nextStart: number
  moved: boolean
}

const TRACK_HEIGHT = 54
const MIN_PX_PER_SEC = 8
const MAX_PX_PER_SEC = 400

function niceTickStep(pxPerSec: number): number {
  const targetPx = 90
  const rawStep = targetPx / pxPerSec
  const candidates = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
  for (const c of candidates) {
    if (c >= rawStep) return c
  }
  return 900
}

export function SubtitleTimeline({
  cues,
  duration,
  currentTime,
  playing,
  selectedCueId,
  disabled,
  labels,
  onSelectCue,
  onSeek,
  onChangeCueTime,
}: SubtitleTimelineProps) {
  const [pxPerSec, setPxPerSec] = useState(50)
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragState | null>(null)

  const safeDuration = Math.max(duration, 1)
  const contentWidth = Math.max(safeDuration * pxPerSec, 200)
  const tickStep = niceTickStep(pxPerSec)

  const ticks = useMemo(() => {
    const out: number[] = []
    for (let tSec = 0; tSec <= safeDuration + 0.0001; tSec += tickStep) {
      out.push(Math.round(tSec * 1000) / 1000)
    }
    return out
  }, [safeDuration, tickStep])

  const neighborBounds = useCallback(
    (id: string, origStart: number) => {
      const others = cues.filter((c) => c.id !== id).sort((a, b) => a.start - b.start)
      const before = [...others].filter((o) => o.start <= origStart).pop()
      const after = others.find((o) => o.start > origStart)
      return {
        prevEnd: before ? before.end : 0,
        nextStart: after ? after.start : safeDuration,
      }
    },
    [cues, safeDuration],
  )

  const beginDrag = useCallback(
    (e: React.PointerEvent, cue: EditableCue, mode: DragMode) => {
      if (disabled) return
      e.stopPropagation()
      e.preventDefault()
      onSelectCue(cue.id)
      const { prevEnd, nextStart } = neighborBounds(cue.id, cue.start)
      dragRef.current = {
        id: cue.id,
        mode,
        startClientX: e.clientX,
        origStart: cue.start,
        origEnd: cue.end,
        prevEnd,
        nextStart,
        moved: false,
      }
      ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
    },
    [disabled, neighborBounds, onSelectCue],
  )

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current
      if (!drag) return
      const deltaSec = (e.clientX - drag.startClientX) / pxPerSec
      if (Math.abs(e.clientX - drag.startClientX) > 2) drag.moved = true
      const dur = drag.origEnd - drag.origStart
      const lowerStart = Math.max(0, drag.prevEnd)
      const upperEnd = Math.min(safeDuration, drag.nextStart)

      if (drag.mode === 'move') {
        const newStart = clamp(drag.origStart + deltaSec, lowerStart, upperEnd - dur)
        onChangeCueTime(drag.id, newStart, newStart + dur)
      } else if (drag.mode === 'resize-start') {
        const newStart = clamp(drag.origStart + deltaSec, lowerStart, drag.origEnd - MIN_CUE_DURATION)
        onChangeCueTime(drag.id, newStart, drag.origEnd)
      } else {
        const newEnd = clamp(drag.origEnd + deltaSec, drag.origStart + MIN_CUE_DURATION, upperEnd)
        onChangeCueTime(drag.id, drag.origStart, newEnd)
      }
    },
    [onChangeCueTime, pxPerSec, safeDuration],
  )

  const endDrag = useCallback((e: React.PointerEvent) => {
    if (!dragRef.current) return
    ;(e.target as HTMLElement).releasePointerCapture?.(e.pointerId)
    dragRef.current = null
  }, [])

  const handleRulerSeek = useCallback(
    (e: React.MouseEvent) => {
      if (dragRef.current?.moved) return
      const content = contentRef.current
      if (!content) return
      const rect = content.getBoundingClientRect()
      const time = clamp((e.clientX - rect.left) / pxPerSec, 0, safeDuration)
      onSeek(time)
    },
    [onSeek, pxPerSec, safeDuration],
  )

  // 播放时让播放头保持在可视区域内
  useEffect(() => {
    if (!playing) return
    const scroller = scrollRef.current
    if (!scroller) return
    const playheadX = currentTime * pxPerSec
    const left = scroller.scrollLeft
    const right = left + scroller.clientWidth
    if (playheadX < left + 40 || playheadX > right - 40) {
      scroller.scrollLeft = Math.max(0, playheadX - scroller.clientWidth / 2)
    }
  }, [currentTime, playing, pxPerSec])

  const zoom = (factor: number) => {
    setPxPerSec((prev) => clamp(Math.round(prev * factor), MIN_PX_PER_SEC, MAX_PX_PER_SEC))
  }

  return (
    <div className="sub-timeline">
      <div className="sub-timeline-toolbar">
        <button type="button" className="sub-timeline-zoom" onClick={() => zoom(0.8)} title={labels.zoomOut}>
          −
        </button>
        <button type="button" className="sub-timeline-zoom" onClick={() => zoom(1.25)} title={labels.zoomIn}>
          ＋
        </button>
      </div>
      <div className="sub-timeline-scroll" ref={scrollRef}>
        <div
          className="sub-timeline-content"
          ref={contentRef}
          style={{ width: `${contentWidth}px` }}
        >
          <div className="sub-timeline-ruler" onMouseDown={handleRulerSeek}>
            {ticks.map((tSec) => (
              <div key={tSec} className="sub-timeline-tick" style={{ left: `${tSec * pxPerSec}px` }}>
                <span className="sub-timeline-tick-label">{formatShortTime(tSec)}</span>
              </div>
            ))}
          </div>
          <div
            className="sub-timeline-track"
            style={{ height: `${TRACK_HEIGHT}px` }}
            onMouseDown={handleRulerSeek}
          >
            {cues.length === 0 ? (
              <div className="sub-timeline-empty">{labels.empty}</div>
            ) : null}
            {cues.map((cue) => {
              const left = cue.start * pxPerSec
              const width = Math.max((cue.end - cue.start) * pxPerSec, 4)
              const isSelected = cue.id === selectedCueId
              const label = cue.text || cue.trans || ''
              return (
                <div
                  key={cue.id}
                  className={`sub-timeline-cue${isSelected ? ' selected' : ''}${disabled ? ' disabled' : ''}`}
                  style={{ left: `${left}px`, width: `${width}px` }}
                  onPointerDown={(e) => beginDrag(e, cue, 'move')}
                  onPointerMove={handlePointerMove}
                  onPointerUp={endDrag}
                  onPointerCancel={endDrag}
                  title={label}
                >
                  <span
                    className="sub-timeline-handle left"
                    onPointerDown={(e) => beginDrag(e, cue, 'resize-start')}
                    onPointerMove={handlePointerMove}
                    onPointerUp={endDrag}
                    onPointerCancel={endDrag}
                  />
                  <span className="sub-timeline-cue-label">{label}</span>
                  <span
                    className="sub-timeline-handle right"
                    onPointerDown={(e) => beginDrag(e, cue, 'resize-end')}
                    onPointerMove={handlePointerMove}
                    onPointerUp={endDrag}
                    onPointerCancel={endDrag}
                  />
                </div>
              )
            })}
          </div>
          <div
            className="sub-timeline-playhead"
            style={{ left: `${currentTime * pxPerSec}px` }}
          />
        </div>
      </div>
    </div>
  )
}
