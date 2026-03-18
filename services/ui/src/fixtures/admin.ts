import type { Tenant, Budget, AgentHealth } from '../types'

export const tenantsFixture: Tenant[] = [
  { id: 't1', name: 'Acme Corp',  status: 'active',    budgetUsed: 42180, budgetLimit: 100000 },
  { id: 't2', name: 'Beta Corp',  status: 'active',    budgetUsed: 8200,  budgetLimit: 50000  },
  { id: 't3', name: 'Gamma Ltd',  status: 'suspended', budgetUsed: 0,     budgetLimit: 50000  },
]

export const budgetsFixture: Budget[] = [
  { tenantId: 't1', tenantName: 'Acme Corp', dailyLimit: 100000, usedToday: 42180, resetsAt: '00:00 UTC' },
  { tenantId: 't2', tenantName: 'Beta Corp', dailyLimit: 50000,  usedToday: 8200,  resetsAt: '00:00 UTC' },
]

export const agentHealthFixture: AgentHealth[] = [
  { name: 'network_agent',    lastHeartbeat: new Date(Date.now() - 5000).toISOString(),   status: 'ok',    p95Ms: 1240 },
  { name: 'rag_agent',        lastHeartbeat: new Date(Date.now() - 12000).toISOString(),  status: 'ok',    p95Ms: 820  },
  { name: 'itsm_agent',       lastHeartbeat: new Date(Date.now() - 45000).toISOString(),  status: 'ok',    p95Ms: 1800 },
  { name: 'enrichment_agent', lastHeartbeat: new Date(Date.now() - 75000).toISOString(),  status: 'stale', p95Ms: 920  },
  { name: 'coordinator',      lastHeartbeat: new Date(Date.now() - 3000).toISOString(),   status: 'ok',    p95Ms: 340  },
]
