import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../../../shared/i18n/useI18n'
import { MarkdownContent } from '../../../shared/ui/MarkdownContent'
import {
  fetchTaskChatHistory,
  saveTaskChatHistory,
  streamTaskChat,
  type ChatMessage,
} from '../../../services/chatApi'

interface ChatPaneProps {
  taskId: string
}

/** 最多把最近若干轮对话作为上下文回传，避免上下文过长。 */
const MAX_HISTORY_TURNS = 6

export function ChatPane({ taskId }: ChatPaneProps) {
  const { t, lang } = useI18n()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const loadGenRef = useRef(0)

  // 切换任务时加载已保存对话，并中断进行中的流
  useEffect(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setInput('')
    setStreaming(false)
    setError(null)
    setMessages([])
    setLoadingHistory(true)

    const gen = ++loadGenRef.current
    void (async () => {
      try {
        const history = await fetchTaskChatHistory(taskId)
        if (loadGenRef.current !== gen) return
        setMessages(history)
      } catch {
        if (loadGenRef.current !== gen) return
        // 加载失败时仍允许新对话，不阻断使用
        setMessages([])
      } finally {
        if (loadGenRef.current === gen) setLoadingHistory(false)
      }
    })()
  }, [taskId])

  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, loadingHistory])

  async function clearChat() {
    if (streaming || loadingHistory || messages.length === 0) return
    setError(null)
    try {
      await saveTaskChatHistory(taskId, [])
      setMessages([])
    } catch {
      setError(t('insightsChatError'))
    }
  }

  async function send() {
    const question = input.trim()
    if (!question || streaming || loadingHistory) return
    setError(null)
    setInput('')

    const history = messages.slice(-MAX_HISTORY_TURNS * 2)
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: question },
      { role: 'assistant', content: '' },
    ])
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller
    try {
      for await (const delta of streamTaskChat(taskId, question, {
        history,
        uiLanguage: lang,
        signal: controller.signal,
      })) {
        setMessages((prev) => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, content: last.content + delta }
          }
          return next
        })
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      setError(t('insightsChatError'))
      // 移除空的 assistant 占位；若用户消息是本轮新增的也一并回滚
      setMessages((prev) => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last && last.role === 'assistant' && !last.content) {
          next.pop()
          const userMsg = next[next.length - 1]
          if (userMsg && userMsg.role === 'user' && userMsg.content === question) {
            next.pop()
          }
        }
        return next
      })
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null
        setStreaming(false)
      }
    }
  }

  return (
    <div className="insights-chat">
      <div className="insights-pane-title-row">
        <div className="insights-pane-title">{t('insightsChatTitle')}</div>
        {messages.length > 0 && !loadingHistory ? (
          <button
            type="button"
            className="insights-chat-clear"
            disabled={streaming}
            onClick={() => void clearChat()}
          >
            {t('insightsChatClear')}
          </button>
        ) : null}
      </div>
      <div className="insights-chat-messages" ref={scrollRef}>
        {loadingHistory ? (
          <div className="insights-chat-empty">{t('insightsChatLoading')}</div>
        ) : messages.length === 0 ? (
          <div className="insights-chat-empty">{t('insightsChatEmpty')}</div>
        ) : (
          messages.map((msg, i) => {
            const thinking =
              !msg.content && streaming && i === messages.length - 1
                ? t('insightsChatThinking')
                : null
            return (
              <div key={i} className={`insights-msg insights-msg-${msg.role}`}>
                {thinking ? (
                  thinking
                ) : msg.role === 'assistant' ? (
                  <MarkdownContent content={msg.content} />
                ) : (
                  msg.content
                )}
              </div>
            )
          })
        )}
      </div>
      {error ? <div className="insights-chat-error-line">{error}</div> : null}
      <div className="insights-chat-input">
        <textarea
          className="insights-chat-textarea"
          rows={2}
          placeholder={t('insightsChatPlaceholder')}
          value={input}
          disabled={streaming || loadingHistory}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <button
          type="button"
          className="insights-chat-send"
          disabled={streaming || loadingHistory || !input.trim()}
          onClick={() => void send()}
        >
          {t('insightsChatSend')}
        </button>
      </div>
    </div>
  )
}
