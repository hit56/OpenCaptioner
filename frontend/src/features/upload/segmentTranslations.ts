import type { UploadSegment } from '../../shared/types/asr'

export interface SegmentTranslationItem {
  index: number
  translation: string
}

export function segmentHasDistinctTranslation(segment: Pick<UploadSegment, 'text' | 'translation'>): boolean {
  const translation = (segment.translation ?? '').trim()
  if (!translation) return false
  return translation !== (segment.text ?? '').trim()
}

export function parseSegmentTranslations(data: unknown): SegmentTranslationItem[] {
  if (!Array.isArray(data)) return []
  return data
    .map((item) => {
      const row = item as Record<string, unknown>
      return {
        index: Number(row.index ?? -1),
        translation: String(row.translation || ''),
      }
    })
    .filter((item) => Number.isFinite(item.index) && item.index >= 0 && item.translation)
}

export function mergeSegmentTranslations(
  segments: UploadSegment[],
  translations: SegmentTranslationItem[],
): UploadSegment[] {
  if (!translations.length) return segments
  const map = new Map(
    translations.map((item) => [Number(item.index), item.translation]),
  )
  return segments.map((segment) => {
    const translation = map.get(Number(segment.index))
    return translation ? { ...segment, translation } : segment
  })
}
