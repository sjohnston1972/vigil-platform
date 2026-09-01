import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { ChatPage } from '../../components/chat/ChatPage'
import { TenantProvider } from '../../context/TenantContext'

// Mock nanoid so new session IDs are deterministic
vi.mock('nanoid', () => ({ nanoid: () => 'test-id-123' }))

function makeSSEStream(events: object[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  const lines = events.map(e => `data: ${JSON.stringify(e)}`)
  return new ReadableStream({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(line + '\n\n'))
      controller.close()
    },
  })
}

function renderChatPage(path = '/chat') {
  return render(
    <TenantProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </TenantProvider>
  )
}

describe('ChatPage', () => {
  beforeEach(() => {
    localStorage.clear()
    // jsdom does not implement scrollIntoView — stub it to avoid TypeError
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
  })

  it('renders session list, chat input, and agent panel', () => {
    renderChatPage('/chat')
    // Session list header
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    // Chat input
    expect(screen.getByRole('textbox', { name: /message input/i })).toBeInTheDocument()
    // Agent panel label
    expect(screen.getByText('AGENTS')).toBeInTheDocument()
  })

  it('creates new session and shows it in list when New chat clicked', async () => {
    renderChatPage('/chat')
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }))
    // After creating a session, it should appear in the session list
    expect(screen.getByText('New conversation')).toBeInTheDocument()
  })

  it('does not render StepUpBanner when no pending approval', () => {
    renderChatPage('/chat')
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
  })

  it('renders the final assistant message exactly once after the stream completes', async () => {
    // TenantProvider fires a fetch('/auth/me') on mount — only intercept /chat/stream
    // so that unrelated calls (e.g. auth bootstrap) don't consume the mocked response.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url === '/chat/stream') {
        return Promise.resolve({
          ok: true,
          body: makeSSEStream([
            { type: 'session_start', session_id: 'session1', tenant_id: 'dev' },
            { type: 'token', content: 'Hel' },
            { type: 'token', content: 'lo there' },
            { type: 'done', tokens_used: 5, session_id: 'session1' },
          ]),
        } as unknown as Response)
      }
      return Promise.resolve({ ok: false, status: 404, statusText: 'Not Found' } as unknown as Response)
    })

    renderChatPage('/chat/session1')
    const input = screen.getByRole('textbox', { name: /message input/i })
    await userEvent.type(input, 'Hi there{Enter}')

    await waitFor(() => {
      expect(screen.getAllByText('Hello there')).toHaveLength(1)
    })
  })
})
