import { getOrCreateClientUserId } from '../shared/storage/clientUser'

export function createSseTaskStream(taskId: string) {
  const userId = encodeURIComponent(getOrCreateClientUserId())
  return new EventSource(`/stream_task/${encodeURIComponent(taskId)}?client_user_id=${userId}`)
}
