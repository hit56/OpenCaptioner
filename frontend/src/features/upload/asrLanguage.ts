export const ASR_LANGUAGE_OPTIONS = [
  { value: 'auto', labelKey: 'asrLangAuto' },
  { value: 'zh', labelKey: 'asrLangZh' },
  { value: 'yue', labelKey: 'asrLangYue' },
  { value: 'en', labelKey: 'asrLangEn' },
  { value: 'ja', labelKey: 'asrLangJa' },
  { value: 'ko', labelKey: 'asrLangKo' },
  { value: 'th', labelKey: 'asrLangTh' },
  { value: 'vi', labelKey: 'asrLangVi' },
] as const

export type AsrLanguageCode = (typeof ASR_LANGUAGE_OPTIONS)[number]['value']

const STORAGE_KEY = 'asr_content_language'
const ALLOWED = new Set<string>(ASR_LANGUAGE_OPTIONS.map((item) => item.value))

export function loadAsrLanguage(): AsrLanguageCode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && ALLOWED.has(saved)) return saved as AsrLanguageCode
  } catch {
    // ignore
  }
  return 'auto'
}

export function saveAsrLanguage(value: AsrLanguageCode) {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // ignore
  }
}

export function asrLanguageLabelKey(code: string): string | undefined {
  if (!code || code === 'auto') return undefined
  return ASR_LANGUAGE_OPTIONS.find((item) => item.value === code)?.labelKey
}
