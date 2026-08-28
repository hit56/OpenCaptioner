import type { UploadSegment } from '../../shared/types/asr'

export function segmentMatchesFilters(
  segment: UploadSegment,
  keyword: string,
  speakerFilter: string | null,
): boolean {
  const normalizedKeyword = keyword.trim().toLowerCase()
  const speakerId =
    segment.speaker !== undefined && segment.speaker !== null ? String(segment.speaker) : 'unknown'

  const haystack = `${segment.text} ${segment.translation ?? ''}`.toLowerCase()
  const matchSearch = !normalizedKeyword || haystack.includes(normalizedKeyword)
  const matchSpeaker = speakerFilter === null || speakerId === speakerFilter
  return matchSearch && matchSpeaker
}
