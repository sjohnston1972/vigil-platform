import { dashboardFixture } from '../../fixtures/dashboard'
import { StatTile } from './StatTile'
import { AttentionBanner } from './AttentionBanner'
import { ActivityFeed } from './ActivityFeed'

export function Dashboard() {
  const stats = dashboardFixture  // TODO: replace with real API call

  return (
    <div className="h-full flex flex-col gap-4 p-6 overflow-y-auto">
      <div className="flex gap-4">
        <StatTile label="Active Sessions"   value={stats.activeSessions}   valueClass="text-green-500" />
        <StatTile label="Pending Approvals" value={stats.pendingApprovals} valueClass="text-amber-500" glowAmber={stats.pendingApprovals > 0} />
        <StatTile label="Failures"          value={stats.failures}         valueClass="text-red-500" />
        <StatTile label="Tokens Today"      value={stats.tokensToday}      valueClass="text-vigil-accent-text" formatK />
      </div>

      <AttentionBanner
        pendingApprovals={stats.pendingApprovals}
        failures={stats.failures}
        onReview={() => { /* TODO: navigate to audit / approvals */ }}
      />

      <ActivityFeed entries={stats.activity} />
    </div>
  )
}
