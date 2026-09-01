export type AgentStatus = 'running' | 'complete' | 'error'

export interface SessionStartEvent { type: 'session_start'; session_id: string; tenant_id: string }
export interface AgentStartEvent { type: 'agent_start'; agent: string; detail?: string }
export interface AgentCompleteEvent { type: 'agent_complete'; agent: string; duration_ms: number }
export interface AgentErrorEvent { type: 'agent_error'; agent: string; error: string }
export interface TokenEvent { type: 'token'; content: string }
export interface StepUpContext {
  device_host?: string
  change_id?: string
  summary?: string
  ticket_id?: string
  action?: string
  tool?: string
  [key: string]: string | undefined
}
export interface ApprovalRequiredEvent {
  type: 'approval_required'
  request_id: string
  tool: string
  context: StepUpContext
  approver_type: 'self' | 'designated'
  expires_at: string
}
export interface ApprovalGrantedEvent { type: 'approval_granted'; request_id: string }
export interface ApprovalRejectedEvent { type: 'approval_rejected'; request_id: string }
export interface ApprovalExpiredEvent { type: 'approval_expired'; request_id: string }
export interface DoneEvent { type: 'done'; tokens_used: number; session_id: string }
export interface ErrorEvent { type: 'error'; code: string; message: string }

export type SSEEvent =
  | SessionStartEvent | AgentStartEvent | AgentCompleteEvent | AgentErrorEvent
  | TokenEvent | ApprovalRequiredEvent | ApprovalGrantedEvent
  | ApprovalRejectedEvent | ApprovalExpiredEvent | DoneEvent | ErrorEvent
