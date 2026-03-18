interface Props {
  pendingApprovals: number
  failures: number
  onReview: () => void
}

export function AttentionBanner({ pendingApprovals, failures, onReview }: Props) {
  if (pendingApprovals === 0 && failures === 0) return null

  const parts: string[] = []
  if (pendingApprovals > 0) parts.push(`${pendingApprovals} approval${pendingApprovals > 1 ? 's' : ''} pending`)
  if (failures > 0) parts.push(`${failures} failure${failures > 1 ? 's' : ''}`)

  return (
    <div className="flex items-center justify-between px-4 py-2 rounded border border-vigil-stepup-border bg-vigil-stepup-bg text-amber-300 text-sm">
      <span>⚠ {parts.join(' · ')}</span>
      <button
        onClick={onReview}
        className="bg-amber-500 text-black text-xs font-bold px-3 py-1 rounded hover:bg-amber-400"
      >
        Review →
      </button>
    </div>
  )
}
