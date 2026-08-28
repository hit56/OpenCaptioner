import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

interface MarkdownContentProps {
  content: string
  className?: string
}

/** 渲染助手/摘要等 Markdown + LaTeX；默认不解析 HTML，避免 XSS。 */
export function MarkdownContent({ content, className }: MarkdownContentProps) {
  return (
    <div className={className ? `md-content ${className}` : 'md-content'}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
        rehypePlugins={[rehypeKatex]}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
