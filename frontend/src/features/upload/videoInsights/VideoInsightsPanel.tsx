import type { UploadTaskResult } from '../../../shared/types/asr'
import { SummaryPane } from './SummaryPane'
import { ChatPane } from './ChatPane'

interface VideoInsightsPanelProps {
  task: UploadTaskResult
}

/**
 * 识别结果下方的左右两栏：左=内容摘要，右=内容问答。
 * 仅在任务完成且已有转写内容时渲染。
 */
export function VideoInsightsPanel({ task }: VideoInsightsPanelProps) {
  const hasTranscript = task.segments.length > 0 || Boolean(task.fullText?.trim())
  if (task.status !== 'done' || !hasTranscript) return null

  return (
    <div className="video-insights">
      <SummaryPane taskId={task.taskId} subtitleVersion={task.subtitleVersion} />
      <ChatPane taskId={task.taskId} />
    </div>
  )
}
