import type { UploadSegment } from '../../shared/types/asr'
import { parseSegmentRange } from './segmentTime'

export interface SpeakerStatItem {
  id: string
  duration: number
  gender?: string | null
}

const VALID_LANGS = new Set([
  'zh', 'yue', 'en', 'ar', 'de', 'fr', 'es', 'pt', 'id', 'it', 'ko', 'ru', 'th', 'vi', 'ja', 'tr', 'hi',
  'ms', 'nl', 'sv', 'da', 'fi', 'pl', 'cs', 'fil', 'fa', 'el', 'ro', 'hu', 'mk',
])

export function isValidDetectedLang(code?: string): boolean {
  return !!code && VALID_LANGS.has(code)
}

export function computeSpeakerDurations(segments: UploadSegment[]): Map<string, number> {
  const durations = new Map<string, number>()
  for (const segment of segments) {
    const spk = segment.speaker !== undefined && segment.speaker !== null ? String(segment.speaker) : 'unknown'
    const range = parseSegmentRange(segment.timestamp)
    if (!range) continue
    durations.set(spk, (durations.get(spk) ?? 0) + (range.end - range.start))
  }
  return durations
}

export function mergeSpeakerStats(
  serverStats: SpeakerStatItem[],
  segments: UploadSegment[],
): SpeakerStatItem[] {
  const liveDurations = computeSpeakerDurations(segments)
  const genderById = new Map(serverStats.map((item) => [String(item.id), item.gender]))
  const ids = new Set<string>()
  serverStats.forEach((item) => ids.add(String(item.id)))
  liveDurations.forEach((_duration, spkId) => ids.add(spkId))

  const merged: SpeakerStatItem[] = []
  for (const id of ids) {
    const duration = liveDurations.get(id) ?? serverStats.find((item) => String(item.id) === id)?.duration ?? 0
    if (duration <= 0.05) continue
    merged.push({
      id,
      duration,
      gender: genderById.get(id) ?? null,
    })
  }

  merged.sort((a, b) => b.duration - a.duration)
  if (merged.length) return merged
  return serverStats.filter((item) => item.duration > 0.05)
}
