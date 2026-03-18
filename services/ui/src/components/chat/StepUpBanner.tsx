import { useState, useEffect } from 'react'

interface Props {
  tool: string
  device?: string
  expiresAt: string
  onApprove: () => void
  onReject: () => void
  onExpire?: () => void
}

function formatCountdown(expiresAt: string): string {
  const secs = Math.max(0, Math.floor((new Date(expiresAt).getTime() - Date.now()) / 1000))
  const m = Math.floor(secs / 60).toString().padStart(2, '0')
  const s = (secs % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

export function StepUpBanner({ tool, device, expiresAt, onApprove, onReject, onExpire }: Props) {
  const [countdown, setCountdown] = useState(() => formatCountdown(expiresAt))

  useEffect(() => {
    const id = setInterval(() => {
      const secs = Math.max(0, Math.floor((new Date(expiresAt).getTime() - Date.now()) / 1000))
      setCountdown(formatCountdown(expiresAt))
      if (secs <= 0) {
        clearInterval(id)
        onExpire?.()
      }
    }, 1000)
    return () => clearInterval(id)
  }, [expiresAt, onExpire])

  return (
    <div className="flex items-center justify-between px-4 py-2 border-b bg-vigil-stepup-bg border-vigil-stepup-border text-sm shrink-0">
      <span className="text-amber-300">
        ⚠ <strong>{tool}</strong> needs approval
        {device && <span className="text-amber-400"> — {device}</span>}
        <span className="text-amber-600 ml-2 text-xs">· expires {countdown}</span>
      </span>
      <div className="flex gap-2">
        <button
          aria-label="approve"
          onClick={onApprove}
          className="bg-green-800 border border-green-600 text-green-300 text-xs px-3 py-1 rounded hover:bg-green-700"
        >
          ✓ Approve
        </button>
        <button
          aria-label="reject"
          onClick={onReject}
          className="bg-red-950 border border-red-800 text-red-400 text-xs px-3 py-1 rounded hover:bg-red-900"
        >
          ✗ Reject
        </button>
      </div>
    </div>
  )
}
