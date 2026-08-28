export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function HighlightedText({ text, keyword }: { text: string; keyword: string }) {
  const trimmed = keyword.trim()
  if (!trimmed) return <>{text}</>

  const regex = new RegExp(`(${escapeRegExp(trimmed)})`, 'gi')
  const parts = text.split(regex)
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <span key={`${index}-${part}`} className="search-highlight">
            {part}
          </span>
        ) : (
          <span key={`${index}-${part}`}>{part}</span>
        ),
      )}
    </>
  )
}
