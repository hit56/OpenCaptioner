import { useRef, useState } from 'react'
import { useI18n } from '../../shared/i18n/useI18n'

interface UploadSearchBarProps {
  keyword: string
  showCopyButton?: boolean
  onKeywordChange: (value: string) => void
  onCopy: (withTime: boolean) => boolean
}

export function UploadSearchBar({
  keyword,
  showCopyButton = false,
  onKeywordChange,
  onCopy,
}: UploadSearchBarProps) {
  const { t } = useI18n()
  const [copyFeedback, setCopyFeedback] = useState<{ text: string; ok: boolean } | null>(null)
  const clickTimerRef = useRef<number | null>(null)

  function showCopyFeedback(ok: boolean, withTime: boolean) {
    setCopyFeedback({ text: withTime ? t('copiedWithTime') : t('copied'), ok })
    window.setTimeout(() => setCopyFeedback(null), 2000)
  }

  function handleCopyClick(withTime: boolean) {
    const ok = onCopy(withTime)
    showCopyFeedback(ok, withTime)
  }

  return (
    <div id="search-area" className="search-container">
      <input
        type="text"
        id="keyword-input"
        placeholder={t('searchPlaceholder')}
        value={keyword}
        onChange={(event) => onKeywordChange(event.target.value)}
      />
      <button
        type="button"
        id="btn-search"
        data-click-action="upload_search"
        data-click-label={t('searchBtn')}
        data-click-tab="upload"
        onClick={() => onKeywordChange(keyword)}
      >
        {t('searchBtn')}
      </button>
      {showCopyButton ? (
        <button
          type="button"
          id="btn-copy"
          className="action-btn"
          data-click-action="upload_copy_result"
          data-click-label={t('copyResult')}
          data-click-tab="upload"
          title={`${t('copyHintSingle')}\n${t('copyHintDouble')}`}
          style={{
            backgroundColor: copyFeedback ? (copyFeedback.ok ? '#4CAF50' : '#F44336') : undefined,
            borderColor: copyFeedback ? (copyFeedback.ok ? '#4CAF50' : '#F44336') : undefined,
            color: copyFeedback ? '#fff' : undefined,
          }}
          onClick={() => {
            if (clickTimerRef.current) clearTimeout(clickTimerRef.current)
            clickTimerRef.current = window.setTimeout(() => handleCopyClick(false), 250)
          }}
          onDoubleClick={(event) => {
            if (clickTimerRef.current) clearTimeout(clickTimerRef.current)
            clickTimerRef.current = null
            event.preventDefault()
            handleCopyClick(true)
          }}
        >
          {copyFeedback?.text || t('copyResult')}
        </button>
      ) : null}
    </div>
  )
}
