interface Props {
  onSearchChange: (v: string) => void
  onAgentChange:  (v: string) => void
  onDateChange:   (v: string) => void
  agents: string[]
}

export function FilterBar({ onSearchChange, onAgentChange, onDateChange, agents }: Props) {
  return (
    <div className="flex gap-2 p-3 border-b border-vigil-border shrink-0">
      <input
        placeholder="Search logs..."
        onChange={e => onSearchChange(e.target.value)}
        className="flex-1 bg-vigil-card border border-vigil-border rounded px-3 py-1.5 text-xs text-vigil-bright placeholder-vigil-muted outline-none focus:border-vigil-accent"
      />
      <select
        onChange={e => onAgentChange(e.target.value)}
        className="bg-vigil-card border border-vigil-border rounded px-2 py-1.5 text-xs text-vigil-body outline-none"
        aria-label="Filter by agent"
      >
        <option value="">All agents</option>
        {agents.map(a => <option key={a} value={a}>{a}</option>)}
      </select>
      <select
        onChange={e => onDateChange(e.target.value)}
        className="bg-vigil-card border border-vigil-border rounded px-2 py-1.5 text-xs text-vigil-body outline-none"
        aria-label="Filter by date"
      >
        <option value="today">Today</option>
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
      </select>
    </div>
  )
}
