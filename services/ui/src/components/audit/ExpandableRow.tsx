import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { AuditEntry } from '../../types'

interface Props { entry: AuditEntry }

export function ExpandableRow({ entry }: Props) {
  const [expanded, setExpanded] = useState(false)

  const borderClass = expanded ? 'border-b border-vigil-accent' : 'border-b border-vigil-border'
  const rowClass = entry.isStepUp
    ? `text-xs ${borderClass} border-l-2 border-amber-500/40 cursor-pointer transition-colors bg-vigil-warn-row`
    : `text-xs ${borderClass} cursor-pointer transition-colors bg-vigil-card hover:bg-vigil-card/80`

  return (
    <>
      <tr
        data-testid="main-row"
        className={rowClass}
        onClick={() => setExpanded(e => !e)}
      >
        <td className="px-3 py-2 text-vigil-muted">{entry.time}</td>
        <td className={`px-3 py-2 ${entry.isStepUp ? 'text-amber-400' : 'text-vigil-accent-text'}`}>
          {entry.agent}
        </td>
        <td className={`px-3 py-2 ${entry.isStepUp ? 'text-amber-300' : 'text-vigil-body'}`}>
          {entry.action}
        </td>
        <td data-testid="tokens-cell" className="px-3 py-2 text-vigil-muted text-right">
          {entry.tokens != null ? entry.tokens.toLocaleString() : '—'}
        </td>
        <td className="px-2 py-2 text-vigil-muted">{expanded ? '▾' : '›'}</td>
      </tr>
      {expanded && (
        <tr className="bg-vigil-sidebar border-b border-vigil-border">
          <td colSpan={5} className="px-4 py-2 text-xs text-vigil-muted">
            <span className="text-vigil-muted">Session:</span>{' '}
            <Link to={`/chat/${entry.sessionId}`} className="text-vigil-accent-text hover:underline">
              {entry.sessionId}
            </Link>
            {entry.agentsInvolved.length > 0 && (
              <>
                {'  '}
                <span className="text-vigil-muted">Agents:</span>{' '}
                <span className="text-vigil-body">{entry.agentsInvolved.join(', ')}</span>
              </>
            )}
            {entry.durationMs != null && (
              <>
                {'  '}
                <span className="text-vigil-muted">Duration:</span>{' '}
                <span className="text-vigil-body">{(entry.durationMs / 1000).toFixed(1)}s</span>
              </>
            )}
          </td>
        </tr>
      )}
    </>
  )
}
