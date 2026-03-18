import { useEffect, useRef } from 'react'
import type { Message } from '../../types'
import { MessageBubble } from './MessageBubble'

interface Props {
  messages: Message[]
  streamingContent: string   // partial assistant message being typed
  isStreaming: boolean
  onSend: (text: string) => void
  dimmed?: boolean
}

export function ChatWindow({ messages, streamingContent, isStreaming, onSend, dimmed }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLInputElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  function handleKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !isStreaming) {
      const input = inputRef.current
      const text = input?.value.trim()
      if (input && text) { onSend(text); input.value = '' }
    }
  }

  return (
    <div className={`flex flex-col flex-1 h-full transition-opacity ${dimmed ? 'opacity-50 pointer-events-none' : ''}`}>
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
        {messages.map((m, i) => <MessageBubble key={i} {...m} />)}
        {streamingContent && <MessageBubble role="assistant" content={streamingContent} />}
        <div ref={bottomRef} />
      </div>
      <div className="p-3 border-t border-vigil-border">
        <input
          ref={inputRef}
          onKeyDown={handleKey}
          disabled={isStreaming}
          placeholder={isStreaming ? 'Waiting for response...' : 'Ask VIGIL anything...'}
          className="w-full bg-vigil-sidebar border border-vigil-border rounded px-3 py-2 text-sm text-vigil-bright placeholder-vigil-muted outline-none focus:border-vigil-accent disabled:opacity-50"
          aria-label="Message input"
        />
      </div>
    </div>
  )
}
