import { useEffect, useState } from 'react'
import { useI18n } from '../../../shared/i18n/useI18n'
import { MarkdownContent } from '../../../shared/ui/MarkdownContent'
import { fetchTaskSummary } from '../../../services/summaryApi'

interface SummaryPaneProps {
  taskId: string
  /** 字幕重新刻印版本号，变化时重新拉取摘要（转写已更新）。 */
  subtitleVersion?: number
}

export function SummaryPane({ taskId, subtitleVersion }: SummaryPaneProps) {
  const { t, lang } = useI18n()
  const [summary, setSummary] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(false)
    void (async () => {
      try {
        const result = await fetchTaskSummary(taskId, lang)
        if (cancelled) return
        setSummary(result.summary)
      } catch {
        if (cancelled) return
        setError(true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [taskId, subtitleVersion, lang, reloadToken])

  return (
    <div className="insights-summary">
      <div className="insights-pane-title">{t('insightsSummaryTitle')}</div>
      <div className="insights-summary-body">
        {loading ? (
          <div className="insights-summary-hint">{t('insightsSummaryLoading')}</div>
        ) : error ? (
          <div className="insights-summary-hint insights-summary-error">
            {t('insightsSummaryError')}
            <button
              type="button"
              className="insights-retry-btn"
              onClick={() => setReloadToken((n) => n + 1)}
            >
              {t('insightsRetry')}
            </button>
          </div>
        ) : (
          <MarkdownContent className="insights-summary-text" content={summary} />
        )}
      </div>
    </div>
  )
}
