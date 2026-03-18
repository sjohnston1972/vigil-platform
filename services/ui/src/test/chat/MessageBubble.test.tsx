import { render, screen } from '@testing-library/react'
import { MessageBubble } from '../../components/chat/MessageBubble'

describe('MessageBubble', () => {
  it('renders content', () => {
    render(<MessageBubble role="user" content="Hello" />)
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })

  it('aligns user message to the right', () => {
    render(<MessageBubble role="user" content="Hi" />)
    expect(screen.getByTestId('bubble-wrapper')).toHaveClass('justify-end')
  })

  it('aligns assistant message to the left', () => {
    render(<MessageBubble role="assistant" content="Hi back" />)
    expect(screen.getByTestId('bubble-wrapper')).toHaveClass('justify-start')
  })

  it('applies assistant colour to assistant bubble', () => {
    render(<MessageBubble role="assistant" content="Response" />)
    expect(screen.getByTestId('bubble')).toHaveClass('text-vigil-msg-text')
  })
})
