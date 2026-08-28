import { useCallback, useEffect, useRef, useState } from 'react'
import { parseSegmentRange } from './segmentTime'

export function useSegmentPlayer() {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const stopTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [playingKey, setPlayingKey] = useState<string | null>(null)

  useEffect(() => {
    const audio = new Audio()
    audioRef.current = audio
    const onPause = () => setPlayingKey(null)
    audio.addEventListener('pause', onPause)
    audio.addEventListener('ended', onPause)
    return () => {
      if (stopTimerRef.current) clearInterval(stopTimerRef.current)
      audio.pause()
      audio.removeEventListener('pause', onPause)
      audio.removeEventListener('ended', onPause)
      audioRef.current = null
    }
  }, [])

  const clearStopScheduler = useCallback(() => {
    if (stopTimerRef.current) {
      clearInterval(stopTimerRef.current)
      stopTimerRef.current = null
    }
  }, [])

  const stop = useCallback(() => {
    clearStopScheduler()
    audioRef.current?.pause()
    setPlayingKey(null)
  }, [clearStopScheduler])

  const togglePlay = useCallback(
    (key: string, timeStr: string, audioSrc: string) => {
      const audio = audioRef.current
      if (!audio || !audioSrc || !timeStr) return
      const range = parseSegmentRange(timeStr)
      if (!range) return

      const fullUrl = new URL(audioSrc, window.location.origin).href
      if (playingKey === key && !audio.paused) {
        stop()
        return
      }

      clearStopScheduler()
      if (audio.src !== fullUrl) audio.src = fullUrl
      setPlayingKey(key)
      audio.currentTime = range.start
      void audio.play().catch(() => setPlayingKey(null))

      stopTimerRef.current = setInterval(() => {
        if (!audioRef.current) return
        if (
          audioRef.current.currentTime >= range.end + 0.6 ||
          audioRef.current.paused ||
          audioRef.current.ended
        ) {
          clearStopScheduler()
          audioRef.current.pause()
          setPlayingKey(null)
        }
      }, 100)
    },
    [clearStopScheduler, playingKey, stop],
  )

  return { playingKey, togglePlay, stop }
}
