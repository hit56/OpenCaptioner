import type { ReactNode } from 'react'

type TabIconName = 'upload' | 'stats'

const paths: Record<TabIconName, ReactNode> = {
  upload: (
    <>
      <path d="M12 16V4" />
      <path d="M8 8l4-4 4 4" />
      <path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />
    </>
  ),
  stats: (
    <>
      <path d="M4 20V10" />
      <path d="M10 20V4" />
      <path d="M16 20v-7" />
      <path d="M2 20h20" />
    </>
  ),
}

export function TabIcon({ name }: { name: TabIconName }) {
  return (
    <span className="tab-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24">{paths[name]}</svg>
    </span>
  )
}
