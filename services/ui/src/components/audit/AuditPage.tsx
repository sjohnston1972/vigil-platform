import { useState, useMemo } from 'react'
import { auditFixture } from '../../fixtures/audit'
import { FilterBar } from './FilterBar'
import { AuditTable } from './AuditTable'

export function AuditPage() {
  const [search, setSearch] = useState('')
  const [agent, setAgent]   = useState('')
  const [date, setDate] = useState('today')

  const agents = useMemo(() => [...new Set(auditFixture.map(e => e.agent))], [])

  const filtered = useMemo(() =>
    auditFixture.filter(e =>
      (!search || e.action.toLowerCase().includes(search.toLowerCase()) || e.agent.includes(search)) &&
      (!agent  || e.agent === agent) &&
      (!date   || true) // date filter placeholder — real filtering wired when API is available
    ), [search, agent, date])

  return (
    <div className="flex flex-col h-full">
      <FilterBar
        onSearchChange={setSearch}
        onAgentChange={setAgent}
        onDateChange={setDate}
        agents={agents}
      />
      <AuditTable entries={filtered} />
    </div>
  )
}
