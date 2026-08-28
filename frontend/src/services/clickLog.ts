import { clientUserIdTag, getOrCreateClientUserId } from '../shared/storage/clientUser'

export type ClickLogMeta = Record<string, string | number | boolean | null | undefined>

export interface ClickLogOptions {
  label?: string
  tab?: string
  taskId?: string
  fileName?: string
  meta?: ClickLogMeta
}

export function logUserClick(action: string, options: ClickLogOptions = {}): void {
  if (!action.trim()) return
  const payload = {
    client_time: new Date().toISOString(),
    client_user_id: getOrCreateClientUserId(),
    user_tag: clientUserIdTag(),
    action,
    label: options.label,
    tab: options.tab,
    task_id: options.taskId,
    file_name: options.fileName,
    path: window.location.pathname,
    meta: options.meta,
  }
  try {
    void fetch('/api/click_log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    })
  } catch {
    // ignore logging failures
  }
}

export function installClickLogCapture(): () => void {
  const handler = (event: MouseEvent) => {
    const target = event.target
    if (!(target instanceof Element)) return
    const el = target.closest('[data-click-action]')
    if (!(el instanceof HTMLElement)) return
    const action = el.dataset.clickAction?.trim()
    if (!action) return
    logUserClick(action, {
      label:
        el.dataset.clickLabel ||
        el.getAttribute('title') ||
        el.textContent?.trim().replace(/\s+/g, ' ').slice(0, 120) ||
        undefined,
      tab: el.dataset.clickTab,
      taskId: el.dataset.taskId,
      fileName: el.dataset.fileName,
    })
  }
  document.addEventListener('click', handler, true)
  return () => document.removeEventListener('click', handler, true)
}
