import type { Message } from '../../types'

export function MessageBubble({ role, content }: Message) {
  const isUser = role === 'user'
  return (
    <div data-testid="bubble-wrapper" className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        data-testid="bubble"
        className={`max-w-[75%] rounded px-3 py-2 text-sm border ${
          isUser
            ? 'bg-vigil-card border-vigil-border text-vigil-body'
            : 'bg-vigil-msg-bg border-indigo-900 text-vigil-msg-text'
        }`}
      >
        {content}
      </div>
    </div>
  )
}
