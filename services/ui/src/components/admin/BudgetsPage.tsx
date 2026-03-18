import { budgetsFixture } from '../../fixtures/admin'

function fmt(n: number) { return Math.round(n / 1000) + 'k' }

function usageClass(used: number, limit: number) {
  const pct = used / limit
  if (pct >= 0.95) return 'text-red-500'
  if (pct > 0.80) return 'text-amber-500'
  return 'text-vigil-body'
}

export function BudgetsPage() {
  return (
    <div>
      <h2 className="text-sm text-vigil-bright font-bold mb-4">Token Budgets</h2>
      <table className="w-full border-collapse">
        <thead>
          <tr className="text-[10px] text-vigil-muted uppercase tracking-widest border-b border-vigil-border">
            <th className="text-left py-2 px-3 font-normal">Tenant</th>
            <th className="text-left py-2 px-3 font-normal">Daily Limit</th>
            <th className="text-left py-2 px-3 font-normal">Used Today</th>
            <th className="text-left py-2 px-3 font-normal">Reset</th>
            <th className="py-2 px-3 font-normal text-left">Edit</th>
          </tr>
        </thead>
        <tbody>
          {budgetsFixture.map(b => (
            <tr key={b.tenantId} className="text-xs border-b border-vigil-border bg-vigil-card">
              <td className="py-2 px-3 text-vigil-bright">{b.tenantName}</td>
              <td className="py-2 px-3 text-vigil-body">{fmt(b.dailyLimit)}</td>
              <td className={`py-2 px-3 ${usageClass(b.usedToday, b.dailyLimit)}`}>
                {fmt(b.usedToday)} / {fmt(b.dailyLimit)}
              </td>
              <td className="py-2 px-3 text-vigil-muted">{b.resetsAt}</td>
              <td className="py-2 px-3">
                <button className="text-vigil-accent-text text-xs hover:underline">Edit</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
