import { useEffect, useRef, useState } from 'react'
import {
  formatCueTime,
  parseCueTime,
  round3,
  type EditableCue,
} from './cueModel'

export interface CueListLabels {
  original: string
  translation: string
  play: string
  splitCue: string
  mergeNext: string
  addAfter: string
  deleteCue: string
  startTime: string
  endTime: string
}

interface SubtitleCueListProps {
  cues: EditableCue[]
  selectedCueId: string | null
  disabled?: boolean
  labels: CueListLabels
  onSelectCue: (id: string) => void
  onSeekToCue: (cue: EditableCue) => void
  onEditText: (id: string, field: 'text' | 'trans', value: string) => void
  onEditTime: (id: string, field: 'start' | 'end', value: number) => void
  onSplit: (id: string) => void
  onMergeNext: (id: string) => void
  onDelete: (id: string) => void
  onAddAfter: (id: string) => void
}

function TimeInput({
  value,
  disabled,
  ariaLabel,
  onCommit,
}: {
  value: number
  disabled?: boolean
  ariaLabel: string
  onCommit: (next: number) => void
}) {
  const [text, setText] = useState(() => formatCueTime(value))
  const [focused, setFocused] = useState(false)

  useEffect(() => {
    if (!focused) setText(formatCueTime(value))
  }, [value, focused])

  const commit = () => {
    const parsed = parseCueTime(text)
    if (parsed == null) {
      setText(formatCueTime(value))
      return
    }
    if (round3(parsed) !== round3(value)) onCommit(parsed)
    else setText(formatCueTime(value))
  }

  return (
    <input
      className="sub-cue-time-input"
      type="text"
      inputMode="decimal"
      aria-label={ariaLabel}
      title={ariaLabel}
      disabled={disabled}
      value={text}
      onFocus={() => setFocused(true)}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        setFocused(false)
        commit()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          ;(e.target as HTMLInputElement).blur()
        }
      }}
    />
  )
}

export function SubtitleCueList({
  cues,
  selectedCueId,
  disabled,
  labels,
  onSelectCue,
  onSeekToCue,
  onEditText,
  onEditTime,
  onSplit,
  onMergeNext,
  onDelete,
  onAddAfter,
}: SubtitleCueListProps) {
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map())

  useEffect(() => {
    if (!selectedCueId) return
    const el = rowRefs.current.get(selectedCueId)
    if (el) el.scrollIntoView({ block: 'nearest' })
  }, [selectedCueId])

  return (
    <div className="sub-cue-list">
      {cues.map((cue, i) => {
        const duration = Math.max(0, cue.end - cue.start)
        const isSelected = cue.id === selectedCueId
        return (
          <div
            key={cue.id}
            ref={(el) => {
              if (el) rowRefs.current.set(cue.id, el)
              else rowRefs.current.delete(cue.id)
            }}
            className={`sub-cue-row${isSelected ? ' selected' : ''}`}
            onMouseDown={() => onSelectCue(cue.id)}
          >
            <div className="sub-cue-head">
              <span className="sub-cue-index">#{i + 1}</span>
              <button
                type="button"
                className="sub-cue-btn play"
                title={labels.play}
                onClick={() => onSeekToCue(cue)}
              >
                ▶
              </button>
              <div className="sub-cue-times">
                <TimeInput
                  value={cue.start}
                  disabled={disabled}
                  ariaLabel={labels.startTime}
                  onCommit={(next) => onEditTime(cue.id, 'start', next)}
                />
                <span className="sub-cue-time-sep">→</span>
                <TimeInput
                  value={cue.end}
                  disabled={disabled}
                  ariaLabel={labels.endTime}
                  onCommit={(next) => onEditTime(cue.id, 'end', next)}
                />
                <span className="sub-cue-duration">{duration.toFixed(2)}s</span>
              </div>
              <div className="sub-cue-actions">
                <button
                  type="button"
                  className="sub-cue-btn"
                  title={labels.splitCue}
                  disabled={disabled}
                  onClick={() => onSplit(cue.id)}
                >
                  ✂
                </button>
                <button
                  type="button"
                  className="sub-cue-btn"
                  title={labels.mergeNext}
                  disabled={disabled || i >= cues.length - 1}
                  onClick={() => onMergeNext(cue.id)}
                >
                  ⬇
                </button>
                <button
                  type="button"
                  className="sub-cue-btn"
                  title={labels.addAfter}
                  disabled={disabled}
                  onClick={() => onAddAfter(cue.id)}
                >
                  ＋
                </button>
                <button
                  type="button"
                  className="sub-cue-btn delete"
                  title={labels.deleteCue}
                  disabled={disabled}
                  onClick={() => onDelete(cue.id)}
                >
                  ✕
                </button>
              </div>
            </div>
            <div className="sub-cue-body">
              <textarea
                className="sub-cue-text"
                value={cue.text}
                placeholder={labels.original}
                disabled={disabled}
                rows={2}
                onChange={(e) => onEditText(cue.id, 'text', e.target.value)}
              />
              <textarea
                className="sub-cue-text translation"
                value={cue.trans}
                placeholder={labels.translation}
                disabled={disabled}
                rows={2}
                onChange={(e) => onEditText(cue.id, 'trans', e.target.value)}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
