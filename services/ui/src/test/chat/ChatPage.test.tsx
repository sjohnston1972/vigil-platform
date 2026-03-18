import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { ChatPage } from '../../components/chat/ChatPage'

// Mock nanoid so new session IDs are deterministic
vi.mock('nanoid', () => ({ nanoid: () => 'test-id-123' }))

function renderChatPage(path = '/chat') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>
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
})
