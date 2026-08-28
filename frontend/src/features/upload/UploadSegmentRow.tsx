import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../../shared/i18n/useI18n'
import type { UploadSegment } from '../../shared/types/asr'
import { HighlightedText } from './highlightKeyword'
import { getSpeakerAvatar } from './speakerAvatar'
import { appendClientUserQuery } from '../../shared/storage/clientUser'
import { segmentHasDistinctTranslation } from './segmentTranslations'

interface UploadSegmentRowProps {
  segment: UploadSegment
  masterAudioUrl: string
  playingKey: string | null
  playKey: string
  keyword: string
  visible: boolean
  /** 是否允许编辑字幕（仅对已完成的视频任务开放） */
  editable?: boolean
  onPlay: (key: string, timestamp: string, audioUrl: string) => void
  onDelete: () => void
  /** 确认编辑某段字幕（原文/译文），由父组件持久化到任务状态并标记为脏段 */
  onEditConfirm?: (index: number, text: string, translation: string) => void
}

export function UploadSegmentRow({
  segment,
  masterAudioUrl,
  playingKey,
  playKey,
  keyword,
  visible,
  editable,
  onPlay,
  onDelete,
  onEditConfirm,
}: UploadSegmentRowProps) {
  const { t } = useI18n()
  const [highlight, setHighlight] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [draftText, setDraftText] = useState(segment.text || '')
  const [draftTranslation, setDraftTranslation] = useState(segment.translation || '')
  const textAreaRef = useRef<HTMLTextAreaElement>(null)

  const isPlaying = playingKey === playKey
  const speakerId =
    segment.speaker !== undefined && segment.speaker !== null ? String(segment.speaker) : undefined

  useEffect(() => {
    if (!segment.final) return
    setHighlight(true)
    const timer = window.setTimeout(() => setHighlight(false), 1200)
    return () => window.clearTimeout(timer)
  }, [segment.final, segment.text, segment.index])

  // 进入编辑模式时，用最新段落内容初始化草稿并聚焦
  useEffect(() => {
    if (isEditing) {
      setDraftText(segment.text || '')
      setDraftTranslation(segment.translation || '')
      const timer = window.setTimeout(() => textAreaRef.current?.focus(), 0)
      return () => window.clearTimeout(timer)
    }
  }, [isEditing]) // eslint-disable-line react-hooks/exhaustive-deps

  const showTranslation = segmentHasDistinctTranslation(segment)
  const rowClass = [
    'segment-row',
    segment.final ? 'seg-final' : 'seg-raw',
    highlight ? 'highlight-anim' : '',
    isPlaying ? 'playing-highlight' : '',
    isEditing ? 'seg-editing' : '',
  ]
    .filter(Boolean)
    .join(' ')

  function beginEdit() {
    if (!editable) return
    setIsEditing(true)
  }

  function cancelEdit() {
    setIsEditing(false)
  }

  function confirmEdit() {
    const text = draftText.trim()
    const trans = draftTranslation.trim()
    if (!text && !trans) {
      // 不允许把整段清空成完全空白
      return
    }
    onEditConfirm?.(segment.index, text, trans)
    setIsEditing(false)
  }

  return (
    <div
      className={rowClass}
      data-speaker={speakerId || 'unknown'}
      id={`seg-${segment.index}`}
      style={{ display: visible ? 'flex' : 'none' }}
    >
      <img
        src={getSpeakerAvatar(speakerId)}
        className="avatar-img"
        alt=""
        onError={(e) => {
          e.currentTarget.style.display = 'none'
        }}
      />
      <div className="segment-meta">
        <div className="seg-top-row">
          <span className="seg-id">#{segment.index + 1}</span>
          {masterAudioUrl && segment.timestamp ? (
            <button
              type="button"
              className={`seg-icon-btn${isPlaying ? ' playing' : ''}`}
              title={t('play')}
              data-click-action="upload_segment_play"
              data-click-label={t('play')}
              data-click-tab="upload"
              onClick={() =>
                onPlay(playKey, segment.timestamp, appendClientUserQuery(masterAudioUrl))
              }
            >
              {isPlaying ? '⏸' : '▶'}
            </button>
          ) : null}
          {segment.segment_url ? (
            <a
              className="seg-icon-btn"
              href={appendClientUserQuery(segment.segment_url)}
              download={`seg_${segment.index}.wav`}
              target="_blank"
              rel="noreferrer"
              title={t('download')}
              data-click-action="upload_segment_download"
              data-click-label={t('download')}
              data-click-tab="upload"
            >
              ⬇
            </a>
          ) : null}
          {editable && !isEditing ? (
            <button
              type="button"
              className="seg-icon-btn edit"
              title={t('editSegment')}
              data-click-action="upload_segment_edit"
              data-click-label={t('editSegment')}
              data-click-tab="upload"
              onClick={beginEdit}
            >
              ✎
            </button>
          ) : null}
          {isEditing ? (
            <>
              <button
                type="button"
                className="seg-icon-btn save"
                title={t('confirmEdit')}
                data-click-action="upload_segment_edit_confirm"
                data-click-label={t('confirmEdit')}
                data-click-tab="upload"
                onClick={confirmEdit}
              >
                ✓
              </button>
              <button
                type="button"
                className="seg-icon-btn cancel"
                title={t('cancelEdit')}
                data-click-action="upload_segment_edit_cancel"
                data-click-label={t('cancelEdit')}
                data-click-tab="upload"
                onClick={cancelEdit}
              >
                ↶
              </button>
            </>
          ) : (
            <button
              type="button"
              className="seg-icon-btn delete"
              title={t('deleteSegment')}
              data-click-action="upload_segment_delete"
              data-click-label={t('deleteSegment')}
              data-click-tab="upload"
              onClick={onDelete}
            >
              ✕
            </button>
          )}
        </div>
        {speakerId ? (
          <div className="seg-speaker" title={`${t('speaker')} ${speakerId}`}>
            Spk {speakerId}
          </div>
        ) : null}
        <div className="seg-time">{segment.timestamp || '--:--'}</div>
      </div>
      <div className={`segment-bilingual${showTranslation ? ' has-translation' : ''}`}>
        {isEditing ? (
          <>
            <textarea
              ref={textAreaRef}
              className="seg-edit-textarea seg-edit-original"
              value={draftText}
              onChange={(e) => setDraftText(e.target.value)}
              placeholder={t('segmentOriginal')}
              rows={2}
            />
            <textarea
              className="seg-edit-textarea seg-edit-translation"
              value={draftTranslation}
              onChange={(e) => setDraftTranslation(e.target.value)}
              placeholder={t('segmentTranslation')}
              rows={2}
            />
          </>
        ) : (
          <>
            <div className="segment-original">
              <HighlightedText text={segment.text} keyword={keyword} />
            </div>
            {showTranslation ? (
              <div className="segment-translation">
                <HighlightedText text={segment.translation!} keyword={keyword} />
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  )
}
