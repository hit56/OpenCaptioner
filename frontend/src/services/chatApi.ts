import { authHeaders } from './authHeaders'
import { getOrCreateClientUserId } from '../shared/storage/clientUser'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface StreamTaskChatOptions {
  history?: ChatMessage[]
  uiLanguage?: string
  signal?: AbortSignal
}

function chatQuery(): string {
  return new URLSearchParams({ client_user_id: getOrCreateClientUserId() }).toString()
}

/** 读取该任务已持久化的问答历史。 */
export async function fetchTaskChatHistory(taskId: string): Promise<ChatMessage[]> {
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/chat?${chatQuery()}`,
    { headers: authHeaders() },
  )
  if (!response.ok) {
    throw new Error('加载对话失败')
  }
  const payload = (await response.json()) as { messages?: ChatMessage[] }
  const messages = Array.isArray(payload.messages) ? payload.messages : []
  return messages.filter(
    (m): m is ChatMessage =>
      !!m &&
      (m.role === 'user' || m.role === 'assistant') &&
      typeof m.content === 'string',
  )
}

/** 覆盖保存对话历史（用于清空等）。 */
export async function saveTaskChatHistory(
  taskId: string,
  messages: ChatMessage[],
): Promise<ChatMessage[]> {
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/chat?${chatQuery()}`,
    {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ messages }),
    },
  )
  if (!response.ok) {
    throw new Error('保存对话失败')
  }
  const payload = (await response.json()) as { messages?: ChatMessage[] }
  return Array.isArray(payload.messages) ? payload.messages : []
}

/**
 * 就视频转写内容进行 RAG 问答，逐块 yield 流式文本增量。
 * 后端为 SSE（POST + text/event-stream），此处用 fetch + ReadableStream 读取，
 * 因为需要 POST 携带问题体，EventSource 仅支持 GET。
 * 流结束后后端会自动把本轮问答写入持久化历史。
 */
export async function* streamTaskChat(
  taskId: string,
  question: string,
  options: StreamTaskChatOptions = {},
): AsyncGenerator<string, void, unknown> {
  const response = await fetch(
    `/task/${encodeURIComponent(taskId)}/chat?${chatQuery()}`,
    {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        question,
        history: options.history ?? [],
        ui_language: options.uiLanguage ?? 'zh-CN',
      }),
      signal: options.signal,
    },
  )
  if (!response.ok || !response.body) {
    throw new Error('问答请求失败')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 事件以空行（\n\n）分隔
    let sepIndex: number
    while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
      const rawEvent = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      for (const line of rawEvent.split('\n')) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const data = trimmed.slice(5).trim()
        if (!data) continue
        let obj: { type?: string; text?: string; message?: string }
        try {
          obj = JSON.parse(data)
        } catch {
          continue
        }
        if (obj.type === 'delta' && obj.text) {
          yield obj.text
        } else if (obj.type === 'error') {
          throw new Error(obj.message || '问答服务异常')
        } else if (obj.type === 'done') {
          return
        }
      }
    }
  }
}
