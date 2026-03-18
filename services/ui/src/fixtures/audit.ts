import type { AuditEntry } from '../types'

export const auditFixture: AuditEntry[] = [
  {
    id: 'a1', time: '14:22:01', agent: 'network_agent',
    action: 'BGP audit — 10.0.0.1', tokens: 1240, isStepUp: false,
    sessionId: 'sess_8f2a', agentsInvolved: ['network_agent', 'rag_agent'], durationMs: 3100,
  },
  {
    id: 'a2', time: '14:17:44', agent: 'itsm_agent',
    action: 'Ticket #4421 created', tokens: 890, isStepUp: false,
    sessionId: 'sess_8f2a', agentsInvolved: ['itsm_agent'], durationMs: 1800,
  },
  {
    id: 'a3', time: '14:10:12', agent: 'step-up',
    action: 'apply_change approved — router-core-01', tokens: null, isStepUp: true,
    sessionId: 'sess_7e1b', agentsInvolved: [], durationMs: null,
  },
  {
    id: 'a4', time: '13:58:30', agent: 'enrichment_agent',
    action: 'CVE-2024-1234 lookup', tokens: 340, isStepUp: false,
    sessionId: 'sess_7e1b', agentsInvolved: ['enrichment_agent'], durationMs: 920,
  },
]
