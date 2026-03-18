import { useNavigate } from 'react-router-dom'
import type { Session } from '../../types'
import { SessionItem } from './SessionItem'

interface Props {
  sessions: Session[]
  activeId: string | undefined
  onRename: (id: string, title: string, tenantId: string) => void
  onNew: () => void
  tenantId?: string
}

export function SessionList({ sessions, activeId, onRename, onNew, tenantId }: Props) {
  const navigate = useNavigate()

  return (
    <div className="w-44 shrink-0 border-r border-vigil-border flex flex-col h-full">
      <div className="px-3 py-2 text-[10px] text-vigil-accent-text uppercase tracking-widest">
        Sessions
      </div>
      <div className="flex-1 overflow-y-auto flex flex-col gap-0.5 px-1">
        {sessions.map(s => (
          <SessionItem
            key={s.id}
            session={s}
            isActive={s.id === activeId}
            onSelect={id => navigate(`/chat/${id}`)}
            onRename={onRename}
            tenantId={tenantId}
          />
        ))}
      </div>
      <div className="p-2 border-t border-vigil-border">
        <button
          onClick={onNew}
          aria-label="New chat"
          className="w-full border border-dashed border-vigil-border text-vigil-muted text-xs py-1.5 rounded hover:text-vigil-body hover:border-vigil-body transition-colors"
        >
          + New chat
        </button>
      </div>
    </div>
  )
}
