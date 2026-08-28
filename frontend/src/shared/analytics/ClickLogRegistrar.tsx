import { useEffect } from 'react'
import { installClickLogCapture } from '../../services/clickLog'

export function ClickLogRegistrar() {
  useEffect(() => installClickLogCapture(), [])
  return null
}
