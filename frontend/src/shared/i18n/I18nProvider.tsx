import { createContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { LangCode } from '../types/asr'
import { translations } from './translations'

interface I18nContextValue {
  lang: LangCode
  setLang: (lang: LangCode) => void
  t: (key: string) => string
}

export const I18nContext = createContext<I18nContextValue | null>(null)

const LANG_KEY = 'app_lang'

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<LangCode>(() => {
    const saved = localStorage.getItem(LANG_KEY)
    if (saved === 'en' || saved === 'zh-CN') return saved
    return 'zh-CN'
  })

  useEffect(() => {
    localStorage.setItem(LANG_KEY, lang)
  }, [lang])

  const value = useMemo<I18nContextValue>(
    () => ({
      lang,
      setLang,
      t: (key: string) => translations[lang][key] ?? translations.en[key] ?? key,
    }),
    [lang],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}
