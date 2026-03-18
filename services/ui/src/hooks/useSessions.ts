import { useState, useCallback } from 'react'
import type { Session } from '../types'

const STORAGE_KEY = 'vigil_sessions'

function load(): Session[] {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]') }
  catch { return [] }
}

function save(sessions: Session[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions))
}

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>(load)

  const createSession = useCallback((id: string, title: string, agents: string[] = []): Session => {
    const s: Session = { id, title, agents, updatedAt: new Date().toISOString() }
    setSessions(prev => {
      const next = [s, ...prev]
      save(next)
      return next
    })
    return s
  }, [])

  const renameSession = useCallback((id: string, title: string) => {
    // TODO: also PATCH /sessions/:id/title when coordinator endpoint exists
    setSessions(prev => {
      const next = prev.map(s => s.id === id ? { ...s, title } : s)
      save(next)
      return next
    })
  }, [])

  return { sessions, createSession, renameSession }
}
