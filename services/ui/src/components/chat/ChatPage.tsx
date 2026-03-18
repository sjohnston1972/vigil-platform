import { useParams, useNavigate } from 'react-router-dom'
import { useSessions } from '../../hooks/useSessions'
import { useStream } from '../../hooks/useStream'
import { SessionList } from './SessionList'
import { ChatWindow } from './ChatWindow'
import { AgentPanel } from './AgentPanel'
import { StepUpBanner } from './StepUpBanner'
import { nanoid } from 'nanoid'

// TODO: replace with real tenant context from JWT claims
const TENANT_ID = 'acme-corp'

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const { sessions, createSession, renameSession } = useSessions()
  const { messages, streamingContent, agentGroups, pendingApproval, totalTokens, isStreaming, sendMessage } = useStream()

  function handleNew() {
    const id = nanoid()
    createSession(id, 'New conversation')
    navigate(`/chat/${id}`)
  }

  function handleSend(text: string) {
    if (!sessionId) return
    sendMessage(text, sessionId, TENANT_ID)
  }

  async function handleApprove() {
    if (!pendingApproval) return
    await fetch(`/step-up/${pendingApproval.requestId}/approve`, { method: 'POST' })
  }

  async function handleReject() {
    if (!pendingApproval) return
    await fetch(`/step-up/${pendingApproval.requestId}/reject`, { method: 'POST' })
  }

  return (
    <div className="flex flex-col h-full">
      {pendingApproval && (
        <StepUpBanner
          tool={pendingApproval.tool}
          device={pendingApproval.device}
          expiresAt={pendingApproval.expiresAt}
          onApprove={handleApprove}
          onReject={handleReject}
          onExpire={handleReject}
        />
      )}
      <div className="flex flex-1 overflow-hidden">
        <SessionList
          sessions={sessions}
          activeId={sessionId}
          onRename={renameSession}
          onNew={handleNew}
        />
        <ChatWindow
          messages={messages}
          streamingContent={streamingContent}
          isStreaming={isStreaming}
          onSend={handleSend}
          dimmed={!!pendingApproval}
        />
        <AgentPanel groups={agentGroups} totalTokens={totalTokens} />
      </div>
    </div>
  )
}
