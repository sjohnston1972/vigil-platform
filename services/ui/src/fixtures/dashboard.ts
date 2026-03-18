import type { DashboardStats } from '../types'

export const dashboardFixture: DashboardStats = {
  activeSessions: 3,
  pendingApprovals: 1,
  failures: 0,
  tokensToday: 42180,
  activity: [
    { id: 'act-1', icon: '✅', description: 'BGP audit — 10.0.0.1', timestamp: '2m ago' },
    { id: 'act-2', icon: '⏳', description: 'Step-up approval pending — router-core-01', timestamp: '5m ago', isStepUp: true },
    { id: 'act-3', icon: '📋', description: 'Ticket #4421 opened in Jira', timestamp: '8m ago' },
    { id: 'act-4', icon: '🔍', description: 'CVE-2024-1234 lookup', timestamp: '18m ago' },
  ],
}
