import { useEffect, useMemo, useState } from 'react'
import type { UploadTaskResult } from '../../shared/types/asr'
import { useI18n } from '../../shared/i18n/useI18n'
import { formatSpkDuration } from './globalStatusUtils'
import {
  formatTaskListTime,
  resolveTaskHistoryStatus,
  resolveTaskMediaDurationSeconds,
} from './taskMedia'
import { formatTaskSidebarHint } from './taskProgress'

interface UploadTaskHistoryPanelProps {
  tasks: UploadTaskResult[]
  selectedTaskId: string | null
  title: string
  statusProcessing: string
  statusPending: string
  statusDone: string
  statusError: string
  onSelect: (taskId: string) => void
  deleteTaskTitle: string
  onDelete: (taskId: string) => void
  /** When set, only this many tasks show until the user clicks "more". */
  collapseLimit?: number
}

function statusLabel(
  task: UploadTaskResult,
  labels: { pending: string; processing: string; done: string; error: string },
): string {
  const status = resolveTaskHistoryStatus(task)
  if (status === 'done') return labels.done
  if (status === 'error') return labels.error
  if (status === 'pending') return labels.pending
  return labels.processing
}

export function UploadTaskHistoryPanel({
  tasks,
  selectedTaskId,
  title,
  statusProcessing,
  statusPending,
  statusDone,
  statusError,
  deleteTaskTitle,
  onSelect,
  onDelete,
  collapseLimit,
}: UploadTaskHistoryPanelProps) {
  const { lang, t } = useI18n()
  const [now, setNow] = useState(() => new Date())
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const sorted = useMemo(
    () =>
      [...tasks].sort((a, b) => {
        const ta = a.createdAt ? Date.parse(a.createdAt) : 0
        const tb = b.createdAt ? Date.parse(b.createdAt) : 0
        if (tb !== ta) return tb - ta
        return b.taskId.localeCompare(a.taskId)
      }),
    [tasks],
  )

  const hasCollapse = collapseLimit != null && collapseLimit > 0
  const hasMore = hasCollapse && sorted.length > collapseLimit
  const visibleTasks =
    hasCollapse && !showAll ? sorted.slice(0, collapseLimit) : sorted

  useEffect(() => {
    if (hasCollapse && sorted.length <= collapseLimit) {
      setShowAll(false)
    }
  }, [collapseLimit, hasCollapse, sorted.length])

  return (
    <aside className={`upload-history-panel${hasMore ? ' upload-history-panel--has-footer' : ''}`}>
      <div className="upload-history-header">{title}</div>
      <div
        className={`upload-history-list${showAll && hasMore ? ' upload-history-list--scrollable' : ''}`}
      >
        {visibleTasks.map((task) => {
          const active = task.taskId === selectedTaskId
          const durationSeconds = resolveTaskMediaDurationSeconds(task)
          const durationLabel = durationSeconds ? formatSpkDuration(durationSeconds) : null
          const progressHint = formatTaskSidebarHint(task, {
            uploading: (percent) => t('uploadingPercent').replace('{0}', String(percent)),
            queuePosition: (position) => t('queuePosition').replace('{0}', String(position)),
          })
          return (
            <div
              key={task.taskId}
              className={`upload-history-item${active ? ' active' : ''}`}
              role="button"
              tabIndex={0}
              data-click-action="upload_history_select"
              data-click-label="选择历史任务"
              data-click-tab="upload"
              data-task-id={task.taskId}
              data-file-name={task.fileName}
              onClick={() => onSelect(task.taskId)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onSelect(task.taskId)
                }
              }}
            >
              <div className="upload-history-item-main">
                <span className="upload-history-name" title={task.fileName}>
                  {task.fileName}
                </span>
                <span
                  className={`upload-history-status status-${resolveTaskHistoryStatus(task)}`}
                >
                  {statusLabel(task, {
                    pending: statusPending,
                    processing: statusProcessing,
                    done: statusDone,
                    error: statusError,
                  })}
                </span>
              </div>
              <div className="upload-history-meta">
                <span className="upload-history-meta-left">
                  <span className="upload-history-time">
                    {formatTaskListTime(task.taskId, task.createdAt, { now, lang })}
                  </span>
                  {progressHint ? (
                    <span className="upload-history-progress-hint">{progressHint}</span>
                  ) : null}
                </span>
                <span className="upload-history-meta-right">
                  <span className="upload-history-meta-label">{t('historyDurationLabel')}</span>
                  <span className="upload-history-duration">{durationLabel || '--'}</span>
                </span>
              </div>
              <button
                type="button"
                className="upload-history-delete"
                title={deleteTaskTitle}
                data-click-action="upload_history_delete"
                data-click-label="删除历史任务"
                data-click-tab="upload"
                data-task-id={task.taskId}
                data-file-name={task.fileName}
                onClick={(e) => {
                  e.stopPropagation()
                  onDelete(task.taskId)
                }}
              >
                ✕
              </button>
            </div>
          )
        })}
      </div>
      {hasMore ? (
        <button
          type="button"
          className="upload-history-more"
          data-click-action="upload_history_more"
          data-click-label={showAll ? t('historyShowLess') : t('historyShowMore')}
          data-click-tab="upload"
          onClick={() => setShowAll((value) => !value)}
        >
          {showAll ? t('historyShowLess') : t('historyShowMore')}
        </button>
      ) : null}
    </aside>
  )
}
