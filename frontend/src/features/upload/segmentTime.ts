export function parseSegmentTime(t: string): number | null {
  if (!t) return null
  const parts = t.trim().split(':')
  if (parts.length !== 2) return null
  return parseInt(parts[0], 10) * 60 + parseFloat(parts[1])
}

export function parseSegmentRange(timeStr: string): { start: number; end: number } | null {
  if (!timeStr || !timeStr.includes('-')) return null
  const [startLabel, endLabel] = timeStr.split('-')
  const start = parseSegmentTime(startLabel)
  const end = parseSegmentTime(endLabel)
  if (start === null || end === null) return null
  return { start, end }
}
