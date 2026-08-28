import { useCallback, useEffect, useRef, useState } from 'react'
import { formatStageTimerText } from './globalStatusUtils'

export interface StageTimerClocks {
  totalStartMs: number
  stageStartMs: number
}

export function useGlobalStageTimer(totalTimeLabel: string) {
  const [timerText, setTimerText] = useState('')
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const totalStartRef = useRef<number | null>(null)
  const stageStartRef = useRef<number | null>(null)

  const tick = useCallback(() => {
    const currentTime = Date.now()
    const stageStart = stageStartRef.current ?? currentTime
    const totalStart = totalStartRef.current ?? currentTime
    const stageElapsed = Math.floor((currentTime - stageStart) / 1000)
    const totalElapsed = Math.floor((currentTime - totalStart) / 1000)
    setTimerText(formatStageTimerText(stageElapsed, totalElapsed, totalTimeLabel))
  }, [totalTimeLabel])

  const stopTimer = useCallback((clearText = false) => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
    if (clearText) setTimerText('')
  }, [])

  const startTimer = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
    const now = Date.now()
    if (totalStartRef.current === null) totalStartRef.current = now
    stageStartRef.current = now
    tick()
    intervalRef.current = setInterval(tick, 500)
  }, [tick])

  const resumeTimer = useCallback(
    (clocks: StageTimerClocks) => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
      totalStartRef.current = clocks.totalStartMs
      stageStartRef.current = clocks.stageStartMs
      tick()
      intervalRef.current = setInterval(tick, 500)
    },
    [tick],
  )

  const resetTimerClocks = useCallback(() => {
    totalStartRef.current = null
    stageStartRef.current = null
  }, [])

  const getTimerClocks = useCallback((): StageTimerClocks | null => {
    if (totalStartRef.current === null || stageStartRef.current === null) return null
    return {
      totalStartMs: totalStartRef.current,
      stageStartMs: stageStartRef.current,
    }
  }, [])

  useEffect(() => () => stopTimer(false), [stopTimer])

  return { timerText, startTimer, resumeTimer, stopTimer, resetTimerClocks, getTimerClocks }
}
