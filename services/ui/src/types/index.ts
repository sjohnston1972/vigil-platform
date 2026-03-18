export interface Session {
  id: string
  title: string
  agents: string[]     // abbreviated agent names e.g. ['net', 'rag']
  updatedAt: string    // ISO string
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
}

export interface ActivityEntry {
  id: string
  icon: string
  description: string
  timestamp: string    // relative e.g. "2m ago"
  isStepUp?: boolean
}

export interface DashboardStats {
  activeSessions: number
  pendingApprovals: number
  failures: number
  tokensToday: number
  activity: ActivityEntry[]
}

export interface AuditEntry {
  id: string
  time: string         // HH:MM:SS
  agent: string
  action: string
  tokens: number | null
  isStepUp: boolean
  sessionId: string
  agentsInvolved: string[]
  durationMs: number | null
}

export interface Tenant {
  id: string
  name: string
  status: 'active' | 'suspended'
  budgetUsed: number
  budgetLimit: number
}

export interface Budget {
  tenantId: string
  tenantName: string
  dailyLimit: number
  usedToday: number
  resetsAt: string
}

export interface AgentHealth {
  name: string
  lastHeartbeat: string  // ISO string
  status: 'ok' | 'stale' | 'down'
  p95Ms: number | null
}

export interface AgentRow {
  agent: string
  status: 'running' | 'complete' | 'error'
  durationMs?: number
  error?: string
}

export interface MessageAgentGroup {
  messageIndex: number
  rows: AgentRow[]
}
