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

  const renameSession = useCallback((id: string, title: string, tenantId: string) => {
    setSessions(prev => {
      const next = prev.map(s => s.id === id ? { ...s, title } : s)
      save(next)
      return next
    })
    try {
      void fetch(`/sessions/${id}/title`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': tenantId },
        body: JSON.stringify({ title }),
      })
    } catch {
      // Coordinator not available in local dev — localStorage is source of truth
    }
  }, [])

  const loadSessions = useCallback(async (tenantId: string) => {
    try {
      const res = await fetch('/sessions', {
        headers: { 'X-Tenant-Id': tenantId },
      })
      if (!res.ok) return
      const data: Session[] = await res.json()
      if (data.length > 0) {
        setSessions(data)
        save(data)
      }
    } catch {
      // Coordinator not available — localStorage is source of truth
    }
  }, [])

  return { sessions, createSession, renameSession, loadSessions }
}
