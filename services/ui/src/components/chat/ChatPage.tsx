import { useParams, useNavigate } from 'react-router-dom'
import { useSessions } from '../../hooks/useSessions'
import { useStream } from '../../hooks/useStream'
import { useTenant } from '../../context/TenantContext'
import { SessionList } from './SessionList'
import { ChatWindow } from './ChatWindow'
import { AgentPanel } from './AgentPanel'
import { StepUpBanner } from './StepUpBanner'
import { nanoid } from 'nanoid'

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const { tenantId } = useTenant()
  const { sessions, createSession, renameSession } = useSessions()
  const { messages, streamingContent, agentGroups, pendingApproval, totalTokens, isStreaming, sendMessage, dismissApproval, addErrorMessage } = useStream()

  function handleNew() {
    const id = nanoid()
    createSession(id, 'New conversation')
    navigate(`/chat/${id}`)
  }

  function handleSend(text: string) {
    if (!sessionId) return
    sendMessage(text, sessionId, tenantId)
  }

  async function handleApprove() {
    if (!pendingApproval) return
    try {
      const res = await fetch(`/step-up/${pendingApproval.requestId}/approve`, {
        method: 'POST',
        headers: { 'X-Tenant-Id': tenantId },
      })
      if (!res.ok) addErrorMessage(`⚠ Approval failed: ${res.status} ${res.statusText}`)
    } catch {
      addErrorMessage('⚠ Approval request failed — network error.')
    }
  }

  async function handleReject() {
    if (!pendingApproval) return
    try {
      const res = await fetch(`/step-up/${pendingApproval.requestId}/reject`, {
        method: 'POST',
        headers: { 'X-Tenant-Id': tenantId },
      })
      if (!res.ok) addErrorMessage(`⚠ Rejection failed: ${res.status} ${res.statusText}`)
    } catch {
      addErrorMessage('⚠ Rejection request failed — network error.')
    }
  }

  return (
    <div className="flex flex-col h-full">
      {pendingApproval && (
        <StepUpBanner
          tool={pendingApproval.tool}
          context={pendingApproval.context}
          approverType={pendingApproval.approverType}
          expiresAt={pendingApproval.expiresAt}
          onApprove={handleApprove}
          onReject={handleReject}
          onExpire={dismissApproval}
        />
      )}
      <div className="flex flex-1 overflow-hidden">
        <SessionList
          sessions={sessions}
          activeId={sessionId}
          onRename={renameSession}
          onNew={handleNew}
          tenantId={tenantId}
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
