import type { AgentHealth } from '../../types/index'
import { agentHealthFixture } from '../../fixtures/admin'

const STATUS_DOT: Record<AgentHealth['status'], string> = {
  ok:    'text-green-500',
  stale: 'text-amber-500',
  down:  'text-red-500',
}

const ROW_CLASS: Record<AgentHealth['status'], string> = {
  ok:    'bg-vigil-card',
  stale: 'bg-vigil-warn-row',
  down:  'bg-red-950/30',
}

function relativeTime(iso: string): string {
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  return `${Math.floor(secs / 60)}m ago`
}

export function AgentHealthPage() {
  return (
    <div>
      <h2 className="text-sm text-vigil-bright font-bold mb-4">Agent Health</h2>
      <table className="w-full border-collapse">
        <thead>
          <tr className="text-[10px] text-vigil-muted uppercase tracking-widest border-b border-vigil-border">
            <th className="text-left py-2 px-3 font-normal">Agent</th>
            <th className="text-left py-2 px-3 font-normal">Status</th>
            <th className="text-left py-2 px-3 font-normal">Last Heartbeat</th>
            <th className="text-left py-2 px-3 font-normal">p95 Response</th>
          </tr>
        </thead>
        <tbody>
          {agentHealthFixture.map(a => (
            <tr key={a.name} className={`text-xs border-b border-vigil-border ${ROW_CLASS[a.status]}`}>
              <td className="py-2 px-3 text-vigil-bright">{a.name}</td>
              <td className={`py-2 px-3 ${STATUS_DOT[a.status]}`}>
                ● {a.status}
              </td>
              <td className="py-2 px-3 text-vigil-muted">{relativeTime(a.lastHeartbeat)}</td>
              <td className="py-2 px-3 text-vigil-body">
                {a.p95Ms != null ? `${a.p95Ms}ms` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
