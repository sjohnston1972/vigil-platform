import { useState } from 'react'
import type { MessageAgentGroup } from '../../types'

const DOT_CLASS: Record<string, string> = {
  running:  'bg-amber-500',
  complete: 'bg-green-500',
  error:    'bg-red-500',
}

interface Props {
  groups: MessageAgentGroup[]
  totalTokens: number
}

export function AgentPanel({ groups, totalTokens }: Props) {
  const [expanded, setExpanded] = useState(false)

  const allDots = groups.flatMap(g => g.rows).map(r => r.status)

  if (!expanded) {
    return (
      <div className="w-7 shrink-0 border-l border-vigil-border flex flex-col items-center py-3 gap-2">
        <span
          className="text-[9px] text-vigil-accent-text uppercase tracking-widest"
          style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
        >
          AGENTS
        </span>
        <div className="flex flex-col gap-1.5 mt-2">
          {allDots.map((s, i) => (
            <div key={i} className={`w-2 h-2 rounded-full ${DOT_CLASS[s]}`} />
          ))}
        </div>
        <button
          aria-label="expand agent panel"
          onClick={() => setExpanded(true)}
          className="mt-auto text-vigil-muted hover:text-vigil-body text-xs"
        >
          ›
        </button>
      </div>
    )
  }

  return (
    <div className="w-36 shrink-0 border-l border-vigil-border flex flex-col py-3 px-2">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[9px] text-vigil-accent-text uppercase tracking-widest">AGENTS</span>
        <button
          aria-label="collapse agent panel"
          onClick={() => setExpanded(false)}
          className="text-vigil-muted hover:text-vigil-body text-xs"
        >
          ‹
        </button>
      </div>

      <div className="flex-1 overflow-y-auto flex flex-col gap-3">
        {groups.map(g => (
          <div key={g.messageIndex}>
            <div className="text-[10px] text-vigil-body mb-1">▾ Message {g.messageIndex}</div>
            {g.rows.map(r => (
              <div key={r.agent} className="flex items-center gap-1.5 pl-2 py-0.5">
                <div
                  data-testid={`dot-${r.agent}`}
                  className={`w-2 h-2 rounded-full shrink-0 ${DOT_CLASS[r.status]}`}
                />
                <span className="text-[10px] text-vigil-body truncate flex-1">{r.agent}</span>
                {r.durationMs && (
                  <span className="text-[9px] text-vigil-muted">
                    {(r.durationMs / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>

      {totalTokens > 0 && (
        <div className="border-t border-vigil-border pt-2 text-[10px] text-vigil-muted">
          {totalTokens.toLocaleString()} tokens
        </div>
      )}
    </div>
  )
}
