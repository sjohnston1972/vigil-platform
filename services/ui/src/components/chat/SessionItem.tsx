import { useState, useRef, useEffect } from 'react'
import type { Session } from '../../types'

interface Props {
  session: Session
  isActive: boolean
  onSelect: (id: string) => void
  onRename: (id: string, title: string) => void
}

export function SessionItem({ session, isActive, onSelect, onRename }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const inputRef = useRef<HTMLInputElement>(null)
  const cancellingRef = useRef(false)

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  useEffect(() => {
    if (!editing) setDraft(session.title)
  }, [session.title, editing])

  function commit() {
    if (!editing) return
    if (cancellingRef.current) {
      cancellingRef.current = false
      return
    }
    const trimmed = draft.trim()
    if (trimmed && trimmed !== session.title) onRename(session.id, trimmed)
    else setDraft(session.title)
    setEditing(false)
  }

  return (
    <div
      data-testid="session-item"
      onClick={() => onSelect(session.id)}
      className={`px-2 py-2 cursor-pointer border-l-2 rounded-r transition-colors ${
        isActive
          ? 'border-vigil-accent bg-vigil-card text-vigil-bright'
          : 'border-transparent hover:bg-vigil-card/50 text-vigil-body'
      }`}
    >
      {editing ? (
        <input
          ref={inputRef}
          aria-label="Rename session"
          className="w-full bg-vigil-border text-vigil-bright text-xs px-1 rounded outline-none"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onClick={e => e.stopPropagation()}
          onBlur={commit}
          onKeyDown={e => {
            if (e.key === 'Enter') { e.currentTarget.blur() }
            if (e.key === 'Escape') { cancellingRef.current = true; setDraft(session.title); setEditing(false); e.currentTarget.blur() }
          }}
        />
      ) : (
        <p
          className="text-xs truncate"
          onDoubleClick={e => { e.stopPropagation(); setEditing(true) }}
        >
          {session.title}
        </p>
      )}
      {session.agents.length > 0 && (
        <div className="flex gap-1 mt-1 flex-wrap">
          {session.agents.map(a => (
            <span key={a} className="text-[10px] text-vigil-muted bg-vigil-border px-1 rounded">
              {a}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
