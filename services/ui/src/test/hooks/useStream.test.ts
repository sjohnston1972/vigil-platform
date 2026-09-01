import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useStream } from '../../hooks/useStream'

function makeStream(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(line + '\n\n'))
      controller.close()
    },
  })
}

function mockFetch(events: object[]) {
  const lines = events.map(e => `data: ${JSON.stringify(e)}`)
  vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
    ok: true,
    body: makeStream(lines),
  } as unknown as Response)
}

describe('useStream', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('accumulates token events into the committed assistant message', async () => {
    mockFetch([
      { type: 'session_start', session_id: 's1', tenant_id: 't1' },
      { type: 'token', content: 'He' },
      { type: 'token', content: 'llo' },
      { type: 'done', tokens_used: 10, session_id: 's1' },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    expect(result.current.messages).toEqual([{ role: 'assistant', content: 'Hello' }])
  })

  it('clears streamingContent once `done` is processed, so the final message is not duplicated', async () => {
    mockFetch([
      { type: 'session_start', session_id: 's1', tenant_id: 't1' },
      { type: 'token', content: 'He' },
      { type: 'token', content: 'llo' },
      { type: 'done', tokens_used: 10, session_id: 's1' },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    // Exactly one assistant message with the concatenated token text...
    const assistantMessages = result.current.messages.filter(m => m.role === 'assistant')
    expect(assistantMessages).toEqual([{ role: 'assistant', content: 'Hello' }])
    // ...and no leftover streaming bubble duplicating it.
    expect(result.current.streamingContent).toBe('')
  })

  it('adds agent rows on agent_start', async () => {
    mockFetch([
      { type: 'agent_start', agent: 'network_agent', detail: '' },
      { type: 'done', tokens_used: 0, session_id: 's1' },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    const rows = result.current.agentGroups.flatMap(g => g.rows)
    expect(rows.some(r => r.agent === 'network_agent' && r.status === 'running')).toBe(true)
  })

  it('updates agent row to complete on agent_complete', async () => {
    mockFetch([
      { type: 'agent_start', agent: 'rag_agent' },
      { type: 'agent_complete', agent: 'rag_agent', duration_ms: 800 },
      { type: 'done', tokens_used: 0, session_id: 's1' },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    const rows = result.current.agentGroups.flatMap(g => g.rows)
    expect(rows.some(r => r.agent === 'rag_agent' && r.status === 'complete')).toBe(true)
  })

  it('sets pendingApproval on approval_required, mapping context and approver_type', async () => {
    mockFetch([
      {
        type: 'approval_required',
        request_id: 'req1',
        tool: 'apply_change',
        context: { device_host: 'r1', change_id: 'chg-42' },
        approver_type: 'designated',
        expires_at: new Date(Date.now() + 60000).toISOString(),
      },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    expect(result.current.pendingApproval).not.toBeNull()
    expect(result.current.pendingApproval?.tool).toBe('apply_change')
    expect(result.current.pendingApproval?.context).toEqual({ device_host: 'r1', change_id: 'chg-42' })
    expect(result.current.pendingApproval?.approverType).toBe('designated')
  })

  it('exposes sessionId from session_start event', async () => {
    mockFetch([
      { type: 'session_start', session_id: 'new-session', tenant_id: 't1' },
      { type: 'done', tokens_used: 0, session_id: 'new-session' },
    ])
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 'new-session', tenant_id: 't1', messages: [] })
    })
    expect(result.current.currentSessionId).toBe('new-session')
  })

  it('shows error message on non-ok HTTP response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: false,
      status: 503,
      statusText: 'Service Unavailable',
    } as unknown as Response)
    const { result } = renderHook(() => useStream())
    await act(async () => {
      await result.current.startStream({ session_id: 's1', tenant_id: 't1', messages: [] })
    })
    expect(result.current.messages.some(m => m.content.includes('503'))).toBe(true)
  })
})
