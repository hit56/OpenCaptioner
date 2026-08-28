import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { appendClientUserQuery } from '../../shared/storage/clientUser'
import type { UploadTaskResult } from '../../shared/types/asr'
import {
  buildSubtitlePreviewVttUrl,
  hasSubtitledVideo,
  isGatewayTaskId,
  isVideoAwaitingSubtitles,
  isVideoFileName,
  resolveVideoPlaybackUrl,
} from './taskMedia'

interface TaskMediaPlayerProps {
  task: UploadTaskResult
  noMediaLabel: string
  videoFallbackHint: string
  subtitledPendingHint: string
  subtitledPlaybackErrorHint: string
  /** 字幕预览轨语言标签（<track srclang/label>） */
  previewTrackLabel?: string
  /** 未保存草稿的字幕预览轨（Blob VTT URL），存在时优先叠加并强制播放原片 */
  draftPreviewVttUrl?: string | null
  /** 暴露当前 video 元素给外部（如字幕时间轴编辑器同步播放头）。 */
  onVideoElement?: (el: HTMLVideoElement | null) => void
}

const MEDIA_ERR_ABORTED = 1

export function TaskMediaPlayer({
  task,
  noMediaLabel,
  videoFallbackHint,
  subtitledPendingHint,
  subtitledPlaybackErrorHint,
  previewTrackLabel,
  draftPreviewVttUrl,
  onVideoElement,
}: TaskMediaPlayerProps) {
  const isVideo = isVideoFileName(task.fileName)
  const subtitledReady = hasSubtitledVideo(task)
  const processingVideo = isVideoAwaitingSubtitles(task)
  const [useVideoSource, setUseVideoSource] = useState(isVideo)
  const [videoError, setVideoError] = useState(false)
  const [subtitledFallback, setSubtitledFallback] = useState(false)
  /** 字幕刚就绪时若用户正在播放原片，先不切源，避免进度被重置 */
  const [holdOriginalPlayback, setHoldOriginalPlayback] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const onVideoElementRef = useRef(onVideoElement)
  onVideoElementRef.current = onVideoElement
  const assignVideoRef = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el
    onVideoElementRef.current?.(el)
  }, [])
  const resumeTimeRef = useRef<number | null>(null)
  const prevVideoUrlRef = useRef<string | undefined>(task.videoUrl)

  // 存在未保存草稿时强制播放原片：成片字幕是旧的，需用原片叠加草稿预览轨
  const hasDraft = Boolean(draftPreviewVttUrl)
  const preferOriginal =
    hasDraft || subtitledFallback || holdOriginalPlayback || !subtitledReady

  const videoSrc = useMemo(
    () =>
      resolveVideoPlaybackUrl(task, {
        preferOriginal,
      }),
    [preferOriginal, task.taskId, task.fileName, task.videoUrl, task.subtitleVersion],
  )

  /**
   * 字幕预览轨仅在播放「原片」时叠加：已烧录成片自带字幕，再叠加会重影。
   * 有未保存草稿时用草稿 Blob VTT；否则用已保存内容的预览端点。
   * 处理中（尚无成片）与回退到原片的场景都归到 preferOriginal。
   */
  const previewVttSrc = useMemo(() => {
    if (!isVideo || !isGatewayTaskId(task.taskId) || !preferOriginal) return ''
    if (draftPreviewVttUrl) return draftPreviewVttUrl
    return buildSubtitlePreviewVttUrl(task.taskId, task.subtitleVersion)
  }, [isVideo, preferOriginal, task.taskId, task.subtitleVersion, draftPreviewVttUrl])

  useEffect(() => {
    setUseVideoSource(isVideo)
    setVideoError(false)
    setSubtitledFallback(false)
    setHoldOriginalPlayback(false)
    resumeTimeRef.current = null
    prevVideoUrlRef.current = task.videoUrl
    // 依赖 subtitleVersion：字幕重新刻印后重置回退/错误状态，重新尝试新的字幕成片，
    // 避免在编辑期间因原片临时加载失败而卡在音频回退，保存后仍无法观看成片。
  }, [isVideo, task.taskId, task.subtitleVersion])

  useEffect(() => {
    const prevVideoUrl = prevVideoUrlRef.current
    prevVideoUrlRef.current = task.videoUrl
    if (prevVideoUrl || !task.videoUrl) return

    const video = videoRef.current
    if (video && !video.paused && !video.ended) {
      setHoldOriginalPlayback(true)
    }
  }, [task.videoUrl])

  // 字幕成片刚就绪：若此前因原片缺失/加载失败回退到音频（如 B 站任务下载期间
  // /media/{taskId} 尚无原片而触发 onError），此处重新切回视频源以播放字幕成片。
  // 依赖 subtitledReady 的 false→true 迁移，仅在成片就绪那一刻触发，不会与
  // handleVideoError 的音频回退相互抖动；正在播放原片时由 holdOriginalPlayback 维持不打断。
  useEffect(() => {
    if (!subtitledReady) return
    setUseVideoSource(isVideo)
    setVideoError(false)
    setSubtitledFallback(false)
  }, [subtitledReady, isVideo])

  // 预览轨 URL 变化（如草稿防抖刷新）时，视频源可能未变、loadedmetadata 不再触发，
  // 这里主动把新轨道设为显示，保证草稿更新即时反映。
  useEffect(() => {
    if (!previewVttSrc) return
    const video = videoRef.current
    if (!video) return
    const enable = () => {
      for (let i = 0; i < video.textTracks.length; i++) {
        video.textTracks[i].mode = 'showing'
      }
    }
    // track 元素刚插入时 textTracks 可能还未就绪，下一帧再试一次
    enable()
    const raf = window.setTimeout(enable, 0)
    return () => window.clearTimeout(raf)
  }, [previewVttSrc])

  const audioSrc = useMemo(
    () => (task.fileUrl ? appendClientUserQuery(task.fileUrl) : ''),
    [task.fileUrl],
  )

  function releaseOriginalHold() {
    const video = videoRef.current
    if (video && video.currentTime > 0) {
      resumeTimeRef.current = video.currentTime
    }
    setHoldOriginalPlayback(false)
  }

  function handleLoadedMetadata() {
    const video = videoRef.current
    // 部分浏览器对动态插入的 <track default> 不会自动启用，这里强制显示预览轨
    if (video && previewVttSrc && video.textTracks.length > 0) {
      video.textTracks[0].mode = 'showing'
    }
    if (!video || resumeTimeRef.current == null) return
    const resumeAt = resumeTimeRef.current
    resumeTimeRef.current = null
    video.currentTime = resumeAt
    void video.play().catch(() => undefined)
  }

  function handleVideoError() {
    const video = videoRef.current
    const errorCode = video?.error?.code
    if (errorCode === MEDIA_ERR_ABORTED) return

    if (subtitledReady && !subtitledFallback) {
      if (video && video.currentTime > 0) {
        resumeTimeRef.current = video.currentTime
      }
      setSubtitledFallback(true)
      setVideoError(false)
      setHoldOriginalPlayback(false)
      return
    }

    setVideoError(true)
    if (!subtitledReady) setUseVideoSource(false)
  }

  if (!audioSrc && !videoSrc) {
    return <div className="task-media-empty">{noMediaLabel}</div>
  }

  if (isVideo && useVideoSource && videoSrc) {
    return (
      <div className="task-media-player">
        <video
          ref={assignVideoRef}
          key={videoSrc}
          className="task-media-video"
          controls
          preload="auto"
          playsInline
          src={videoSrc}
          onLoadedMetadata={handleLoadedMetadata}
          onPlaying={() => setVideoError(false)}
          onPause={() => {
            if (holdOriginalPlayback && subtitledReady && !subtitledFallback) {
              releaseOriginalHold()
            }
          }}
          onEnded={() => {
            if (holdOriginalPlayback) releaseOriginalHold()
          }}
          onError={handleVideoError}
        >
          {previewVttSrc ? (
            <track
              key={previewVttSrc}
              kind="subtitles"
              src={previewVttSrc}
              srcLang="zh"
              label={previewTrackLabel || 'Subtitles'}
              default
            />
          ) : null}
        </video>
        {(processingVideo || holdOriginalPlayback) && !videoError ? (
          <p className="task-media-fallback-hint">{subtitledPendingHint}</p>
        ) : null}
        {subtitledFallback && subtitledReady && !videoError ? (
          <p className="task-media-fallback-hint">{subtitledPlaybackErrorHint}</p>
        ) : null}
        {videoError && !subtitledReady ? (
          <p className="task-media-fallback-hint">{videoFallbackHint}</p>
        ) : null}
        {videoError && subtitledReady && subtitledFallback ? (
          <p className="task-media-fallback-hint">{videoFallbackHint}</p>
        ) : null}
      </div>
    )
  }

  if (!audioSrc) {
    return <div className="task-media-empty">{noMediaLabel}</div>
  }

  return (
    <div className="task-media-player">
      <audio
        key={audioSrc}
        className="task-media-audio"
        controls
        preload="metadata"
        src={audioSrc}
      />
      {isVideo && !subtitledReady ? (
        <p className="task-media-fallback-hint">{videoFallbackHint}</p>
      ) : null}
    </div>
  )
}
