import type { GlobalStatusState } from './globalStatusUtils'

interface GlobalStatusDashboardProps {
  status: GlobalStatusState
  timerText: string
  statusReady: string
  waitingTask: string
}

export function GlobalStatusDashboard({
  status,
  timerText,
  statusReady,
  waitingTask,
}: GlobalStatusDashboardProps) {
  const processing = status.percentVal < 100
  return (
    <div
      id="global-status-dashboard"
      className="status-dashboard"
      style={{ display: status.visible ? 'block' : 'none' }}
    >
      <div className="status-header">
        <span id="global-status-title" className="status-title">
          {status.title || statusReady}
        </span>
        <span id="global-status-percent" className="status-badge">
          {status.percentText}
        </span>
      </div>
      <div className="progress-track">
        <div
          id="global-progress-fill"
          className={`progress-fill${processing ? ' processing' : ''}`}
          style={{ width: `${status.percentVal}%` }}
        />
      </div>
      <div className="status-footer">
        <span id="global-status-left">{status.detailLeft || waitingTask}</span>
        <span className="status-right-wrap">
          <span id="global-stage-timer" className="stage-timer">
            {timerText}
          </span>
        </span>
      </div>
    </div>
  )
}
