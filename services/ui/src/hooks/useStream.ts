import { useState, useCallback, useRef, useEffect } from 'react'
import type { SSEEvent } from '../types/sse'
import type { Message, MessageAgentGroup, AgentRow } from '../types'

interface ChatRequest {
  session_id: string
  tenant_id: string
  messages: Message[]
}

interface PendingApproval {
  requestId: string
  tool: string
  device?: string
  expiresAt: string
}

export function useStream() {
  const [messages,         setMessages]         = useState<Message[]>([])
  const [streamingContent, setStreamingContent] = useState('')
  const [agentGroups,      setAgentGroups]      = useState<MessageAgentGroup[]>([])
  const [pendingApproval,  setPendingApproval]  = useState<PendingApproval | null>(null)
  const [totalTokens,      setTotalTokens]      = useState(0)
  const [isStreaming,      setIsStreaming]       = useState(false)
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)

  // Ref so startStream can read the current messages without them being a dependency
  const messagesRef = useRef<Message[]>([])

  useEffect(() => { messagesRef.current = messages }, [messages])

  const startStream = useCallback(async (request: ChatRequest) => {
    setIsStreaming(true)
    setStreamingContent('')

    // Track which message index we're on (increments each time a user message is sent)
    const messageIdx = messagesRef.current.filter(m => m.role === 'user').length + 1

    let buffer = ''
    let accumulated = ''

    try {
      const response = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      })

      if (!response.ok) {
        setMessages(prev => [...prev, { role: 'assistant', content: `⚠ Gateway error: ${response.status} ${response.statusText}` }])
        setIsStreaming(false)
        return
      }
      if (!response.body) {
        setMessages(prev => [...prev, { role: 'assistant', content: '⚠ No response body received.' }])
        setIsStreaming(false)
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop()!

        for (const part of parts) {
          if (!part.startsWith('data: ')) continue
          let event: SSEEvent
          try { event = JSON.parse(part.slice(6)) }
          catch { continue }

          switch (event.type) {
            case 'session_start':
              setCurrentSessionId(event.session_id)
              break

            case 'token':
              accumulated += event.content
              setStreamingContent(accumulated)
              break

            case 'agent_start':
              setAgentGroups(prev => {
                const existing = prev.find(g => g.messageIndex === messageIdx)
                const row: AgentRow = { agent: event.agent, status: 'running' }
                if (existing) return prev.map(g => g.messageIndex === messageIdx ? { ...g, rows: [...g.rows, row] } : g)
                return [...prev, { messageIndex: messageIdx, rows: [row] }]
              })
              break

            case 'agent_complete':
              setAgentGroups(prev => prev.map(g =>
                g.messageIndex === messageIdx
                  ? { ...g, rows: g.rows.map(r => r.agent === event.agent ? { ...r, status: 'complete', durationMs: event.duration_ms } : r) }
                  : g
              ))
              break

            case 'agent_error':
              setAgentGroups(prev => prev.map(g =>
                g.messageIndex === messageIdx
                  ? { ...g, rows: g.rows.map(r => r.agent === event.agent ? { ...r, status: 'error', error: event.error } : r) }
                  : g
              ))
              break

            case 'approval_required':
              setPendingApproval({ requestId: event.request_id, tool: event.tool, device: event.device, expiresAt: event.expires_at })
              break

            case 'approval_granted':
            case 'approval_rejected':
            case 'approval_expired': {
              setPendingApproval(null)
              const msg = event.type === 'approval_granted'
                ? '[Approval granted — continuing...]'
                : event.type === 'approval_rejected'
                  ? '[Approval rejected — action cancelled.]'
                  : '[Approval expired — action timed out.]'
              setMessages(prev => [...prev, { role: 'assistant', content: msg }])
              break
            }

            case 'done':
              setMessages(prev => [...prev, { role: 'assistant', content: accumulated }])
              // Do NOT clear streamingContent here — leave it for observers to read
              // It will be reset at the start of the next startStream call
              setTotalTokens(t => t + event.tokens_used)
              setIsStreaming(false)
              break

            case 'error':
              setMessages(prev => [...prev, { role: 'assistant', content: `⚠ Error: ${event.message}` }])
              setIsStreaming(false)
              break
          }
        }
      }
    } catch {
      setMessages(prev => [...prev, { role: 'assistant', content: '⚠ Connection error.' }])
      setIsStreaming(false)
    }
  }, [])

  const sendMessage = useCallback((text: string, sessionId: string, tenantId: string) => {
    const userMsg: Message = { role: 'user', content: text }
    const updated = [...messagesRef.current, userMsg]
    messagesRef.current = updated
    setMessages(updated)
    startStream({ session_id: sessionId, tenant_id: tenantId, messages: updated })
  }, [startStream])

  return { messages, streamingContent, agentGroups, pendingApproval, totalTokens, isStreaming, currentSessionId, startStream, sendMessage }
}
