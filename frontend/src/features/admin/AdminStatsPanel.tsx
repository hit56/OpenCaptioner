import { useCallback, useEffect, useState } from 'react'
import { fetchOperationStats, type OperationStats } from '../../services/adminApi'
import { formatSpkDuration } from '../upload/formatSpkDuration'
import { useI18n } from '../../shared/i18n/useI18n'

interface AdminStatsPanelProps {
  open: boolean
  onClose: () => void
}

function formatLastActive(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

export function AdminStatsPanel({ open, onClose }: AdminStatsPanelProps) {
  const { t } = useI18n()
  const [stats, setStats] = useState<OperationStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchOperationStats()
      setStats(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('adminLoadError'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  if (!open) return null

  return (
    <div className="lang-modal-overlay admin-stats-overlay active" role="presentation" onClick={onClose}>
      <div
        className="lang-modal admin-stats-modal"
        role="dialog"
        aria-modal="true"
        aria-label={t('adminStats')}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="lang-modal-header">
          <h3>{t('adminStats')}</h3>
          <button
            type="button"
            className="lang-modal-close"
            aria-label={t('close')}
            data-click-action="admin_stats_close"
            data-click-label="关闭运营数据"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <div className="lang-modal-body admin-stats-body">
          {loading ? <div className="admin-stats-hint">{t('adminLoading')}</div> : null}
          {error ? (
            <div className="admin-stats-error">
              {error}
              <button
                type="button"
                className="admin-stats-retry"
                data-click-action="admin_stats_retry"
                data-click-label="重试加载运营数据"
                onClick={() => void load()}
              >
                {t('adminRetry')}
              </button>
            </div>
          ) : null}
          {!loading && !error && stats ? (
            <>
              <div className="admin-stats-summary">
                <div className="admin-stat-card">
                  <span className="admin-stat-value">{formatSpkDuration(stats.totalDurationSeconds)}</span>
                  <span className="admin-stat-label">{t('adminTotalDuration')}</span>
                </div>
                <div className="admin-stat-card">
                  <span className="admin-stat-value">{stats.totalUsers}</span>
                  <span className="admin-stat-label">{t('adminTotalUsers')}</span>
                </div>
                <div className="admin-stat-card">
                  <span className="admin-stat-value">{stats.totalTasks}</span>
                  <span className="admin-stat-label">{t('adminTotalTasks')}</span>
                </div>
              </div>

              <h4 className="admin-stats-subtitle">{t('adminPerUser')}</h4>
              {stats.users.length === 0 ? (
                <div className="admin-stats-hint">{t('adminNoData')}</div>
              ) : (
                <div className="admin-stats-table-wrap">
                  <table className="admin-stats-table">
                    <thead>
                      <tr>
                        <th>{t('adminUser')}</th>
                        <th>{t('adminUsageCount')}</th>
                        <th>{t('adminDuration')}</th>
                        <th>{t('adminLastActive')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.users.map((user) => (
                        <tr key={user.userId}>
                          <td>
                            <span className="admin-user-name">{user.displayName}</span>
                            {user.userName ? (
                              <span className="admin-user-id">{`ID：${user.userName}`}</span>
                            ) : null}
                          </td>
                          <td>{user.taskCount}</td>
                          <td>{formatSpkDuration(user.totalDurationSeconds)}</td>
                          <td>{formatLastActive(user.lastActive)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}
