export function formatStageTimerValue(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(Number(seconds) || 0))
  const h = Math.floor(safeSeconds / 3600)
  const m = Math.floor((safeSeconds % 3600) / 60)
  const s = safeSeconds % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function formatStageTimerText(
  elapsedSeconds: number,
  totalSeconds: number,
  totalTimeLabel: string,
): string {
  const safeElapsed = Math.max(0, Math.floor(Number(elapsedSeconds) || 0))
  const safeTotal = Math.max(safeElapsed, Math.floor(Number(totalSeconds) || 0))
  return `⏱ ${formatStageTimerValue(safeElapsed)} / ${totalTimeLabel} ${formatStageTimerValue(safeTotal)}`
}

export function formatSpkDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s'
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function parseDurationToSeconds(durationText: unknown): number {
  if (typeof durationText === 'number' && Number.isFinite(durationText)) return durationText
  const raw = String(durationText || '').trim()
  if (!raw) return 0
  if (/^\d+(?:\.\d+)?s?$/.test(raw)) {
    return parseFloat(raw) || 0
  }
  let total = 0
  const hourMatch = raw.match(/(\d+(?:\.\d+)?)\s*h/i)
  const minMatch = raw.match(/(\d+(?:\.\d+)?)\s*m/i)
  const secMatch = raw.match(/(\d+(?:\.\d+)?)\s*s/i)
  if (hourMatch) total += parseFloat(hourMatch[1]) * 3600
  if (minMatch) total += parseFloat(minMatch[1]) * 60
  if (secMatch) total += parseFloat(secMatch[1])
  return Number.isFinite(total) ? total : 0
}

export interface GlobalStatusState {
  visible: boolean
  title: string
  percentText: string
  percentVal: number
  detailLeft: string
}

export const INITIAL_GLOBAL_STATUS: GlobalStatusState = {
  visible: false,
  title: '',
  percentText: '0%',
  percentVal: 0,
  detailLeft: '',
}

export function patchGlobalStatus(
  prev: GlobalStatusState,
  patch: {
    visible?: boolean
    title?: string | null
    percentText?: string | null
    percentVal?: number
    detailLeft?: string | null
  },
): GlobalStatusState {
  const next = { ...prev }
  if (patch.visible !== undefined) next.visible = patch.visible
  if (patch.title !== undefined && patch.title !== null) next.title = patch.title
  if (patch.percentText !== undefined && patch.percentText !== null) next.percentText = patch.percentText
  if (patch.percentVal !== undefined) next.percentVal = patch.percentVal
  if (patch.detailLeft !== undefined && patch.detailLeft !== null) next.detailLeft = patch.detailLeft
  return next
}
