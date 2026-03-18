import type { ActivityEntry } from '../../types'

interface Props { entries: ActivityEntry[] }

export function ActivityFeed({ entries }: Props) {
  return (
    <div className="bg-vigil-card border border-vigil-border rounded p-4 flex-1 overflow-y-auto">
      <div className="text-xs text-vigil-accent-text uppercase tracking-widest mb-3">
        Recent Activity
      </div>
      <div className="flex flex-col gap-1.5">
        {entries.map((e, i) => (
          <div
            key={i}
            data-testid="activity-row"
            className={`flex items-center gap-2 text-sm ${e.isStepUp ? 'text-amber-400' : 'text-vigil-body'}`}
          >
            <span>{e.icon}</span>
            <span className="flex-1">{e.description}</span>
            <span className="text-vigil-muted text-xs shrink-0">{e.timestamp}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
