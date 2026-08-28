import type { SubtitleCuePayload } from '../../../services/apiClient'

/** 编辑器内部的字幕条模型：带稳定 id，便于拖拽/编辑时保持引用。 */
export interface EditableCue {
  id: string
  start: number
  end: number
  text: string
  trans: string
}

/** 单条字幕的最短时长（秒），避免把一条拖成 0 长度不可见。 */
export const MIN_CUE_DURATION = 0.2

let cueIdSeq = 0

export function createCueId(): string {
  cueIdSeq += 1
  return `cue_${Date.now().toString(36)}_${cueIdSeq}`
}

export function toEditableCues(payload: SubtitleCuePayload[]): EditableCue[] {
  return payload
    .map((c) => ({
      id: createCueId(),
      start: Number(c.start) || 0,
      end: Math.max(Number(c.end) || 0, Number(c.start) || 0),
      text: c.text ?? '',
      trans: c.trans ?? '',
    }))
    .sort((a, b) => a.start - b.start || a.end - b.end)
}

export function toCuePayload(cues: EditableCue[]): SubtitleCuePayload[] {
  return [...cues]
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((c) => ({
      start: round3(c.start),
      end: round3(c.end),
      text: c.text.trim(),
      trans: c.trans.trim(),
    }))
    .filter((c) => c.text || c.trans)
}

export function round3(value: number): number {
  return Math.round(value * 1000) / 1000
}

export function clamp(value: number, min: number, max: number): number {
  if (value < min) return min
  if (value > max) return max
  return value
}

/** 秒 → mm:ss.mmm（超过一小时用 h:mm:ss.mmm）。 */
export function formatCueTime(seconds: number): string {
  const s = Math.max(0, seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  const ms = Math.round((s - Math.floor(s)) * 1000)
  const mmss = `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
  return h > 0 ? `${h}:${mmss}` : mmss
}

/** 解析 mm:ss.mmm / h:mm:ss.mmm / ss.mmm 为秒；无法解析返回 null。 */
export function parseCueTime(input: string): number | null {
  const raw = input.trim()
  if (!raw) return null
  const parts = raw.split(':')
  if (parts.length > 3) return null
  let seconds = 0
  for (let i = 0; i < parts.length; i++) {
    const value = Number(parts[i])
    if (!Number.isFinite(value) || value < 0) return null
    seconds = seconds * 60 + value
  }
  return Number.isFinite(seconds) ? seconds : null
}

/** 短标签（时间轴/紧凑显示）：mm:ss。 */
export function formatShortTime(seconds: number): string {
  const s = Math.max(0, seconds)
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

export function cuesEqual(a: EditableCue[], b: EditableCue[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const x = a[i]
    const y = b[i]
    if (
      x.id !== y.id ||
      round3(x.start) !== round3(y.start) ||
      round3(x.end) !== round3(y.end) ||
      x.text !== y.text ||
      x.trans !== y.trans
    ) {
      return false
    }
  }
  return true
}
