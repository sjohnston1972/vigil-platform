import { useState, useEffect } from 'react'
import type { StepUpContext } from '../../types/sse'

interface Props {
  tool: string
  context?: StepUpContext
  approverType?: 'self' | 'designated'
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

/** Human-readable descriptor built from the step-up context, e.g. "router-core-01" or "change chg-42". */
function describeContext(context?: StepUpContext): string | undefined {
  if (!context) return undefined
  if (context.device_host) return context.device_host
  if (context.change_id) return `change ${context.change_id}`
  if (context.ticket_id) return `ticket ${context.ticket_id}`
  if (context.action) return context.action
  if (context.summary) return context.summary
  return undefined
}

const APPROVER_TYPE_LABEL: Record<'self' | 'designated', string> = {
  self: 'self-approval allowed',
  designated: 'designated approver required',
}

export function StepUpBanner({ tool, context, approverType, expiresAt, onApprove, onReject, onExpire }: Props) {
  const descriptor = describeContext(context)
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
        {descriptor && <span className="text-amber-400"> — {descriptor}</span>}
        {approverType && <span className="text-amber-600 ml-2 text-xs">({APPROVER_TYPE_LABEL[approverType]})</span>}
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
