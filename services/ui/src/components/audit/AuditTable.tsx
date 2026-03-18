import type { AuditEntry } from '../../types'
import { ExpandableRow } from './ExpandableRow'

interface Props { entries: AuditEntry[] }

export function AuditTable({ entries }: Props) {
  return (
    <div className="overflow-y-auto flex-1">
      <table className="w-full border-collapse">
        <thead className="sticky top-0 bg-vigil-sidebar">
          <tr className="text-[10px] text-vigil-muted uppercase tracking-widest">
            <th className="px-3 py-2 text-left font-normal">Time</th>
            <th className="px-3 py-2 text-left font-normal">Agent</th>
            <th className="px-3 py-2 text-left font-normal">Action</th>
            <th className="px-3 py-2 text-right font-normal">Tokens</th>
            <th className="px-2 py-2" />
          </tr>
        </thead>
        <tbody>
          {entries.map(e => <ExpandableRow key={e.id} entry={e} />)}
        </tbody>
      </table>
    </div>
  )
}
